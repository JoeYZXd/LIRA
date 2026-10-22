"""Local textbook drafts: bounded rendering, explicit review, atomic publication.

Call rendering from a worker thread. A store serializes its mutations; the web
application should share one instance. Recognition never writes the voice library.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from PIL import Image, ImageOps


MAX_BYTES = 100 * 1024 * 1024
MAX_PAGES = 200
HANZI = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ebef]')


def require(condition, message='教材数据无效'):
    if not condition:
        raise ValueError(message)


class TextbookImportStore:
    def __init__(self, root: Path, books_dir: Path):
        require(not Path(root).is_symlink() and not Path(books_dir).is_symlink())
        self.root = Path(root).resolve()
        self.books_dir = Path(books_dir).resolve()
        require(self.root != self.books_dir and self.books_dir not in self.root.parents
                and self.root not in self.books_dir.parents, '草稿与教材库必须分开存放')
        self.root.mkdir(parents=True, exist_ok=True)
        self.books_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _folder(self, draft_id):
        require(isinstance(draft_id, str) and re.fullmatch(r'[a-f0-9]{32}', draft_id))
        path = self.root / draft_id
        require(not path.is_symlink() and path.resolve().parent == self.root)
        return path

    @staticmethod
    def _write(path, value):
        require(not path.is_symlink())
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
        require(len(raw) <= 16 * 1024 * 1024, '教材数据过大')
        temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
        try:
            with temp.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def get(self, draft_id):
        path = self._folder(draft_id) / 'draft.json'
        require(not path.is_symlink())
        with path.open('rb') as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        require(len(raw) <= 16 * 1024 * 1024)
        draft = json.loads(raw)
        # Legacy imports with no cover choice use their first uploaded page.
        # Explicit null still means a title-only cover.
        draft.setdefault('cover_page', 1 if draft.get('pages') else None)
        return draft

    def list_drafts(self):
        result = []
        for path in self.root.iterdir():
            if path.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', path.name):
                continue
            try:
                draft = self.get(path.name)
                cover = draft.get('cover_page')
                if type(cover) is not int or not 1 <= cover <= len(draft['pages']):
                    cover = None
                result.append(dict({k: draft[k] for k in ('id', 'title', 'status', 'revision')},
                                   page_count=len(draft['pages']), cover_page=cover))
            except (ValueError, OSError, KeyError):
                continue
        return result

    def create(self, files):
        require(isinstance(files, list) and 1 <= len(files) <= MAX_PAGES, '请选择 PDF 或图片')
        require(all(isinstance(f, (list, tuple)) and len(f) == 2 and
                    isinstance(f[0], str) and isinstance(f[1], bytes) for f in files))
        require(sum(len(raw) for _, raw in files) <= MAX_BYTES, '上传总大小不能超过 100 MiB')
        draft_id = uuid.uuid4().hex
        folder = self._folder(draft_id)
        folder.mkdir()
        draft = dict(id=draft_id, status='uploaded', revision=1, imported_at=time.time(), title='', lessons=[],
                     pages=[], warnings=[], error='')

        def add_page(image, text=''):
            require(len(draft['pages']) < MAX_PAGES, '教材最多支持 200 页')
            number = len(draft['pages']) + 1
            image.thumbnail((1600, 1600))
            image.convert('RGB').save(folder / f'{number}.png', 'PNG')
            draft['pages'].append(dict(number=number, text=text[:30000],
                image_url=f'/dev/api/textbooks/drafts/{draft_id}/pages/{number}'))

        try:
            for _, raw in files:
                if raw.startswith(b'%PDF-'):
                    import pypdfium2 as pdfium
                    pdf = pdfium.PdfDocument(raw)
                    try:
                        require(0 < len(pdf) <= MAX_PAGES - len(draft['pages']), '教材最多支持 200 页')
                        for index in range(len(pdf)):
                            page = pdf[index]
                            try:
                                width, height = page.get_size()
                                require(width > 0 and height > 0)
                                textpage = page.get_textpage()
                                try:
                                    text = textpage.get_text_range()[:30000]
                                finally:
                                    textpage.close()
                                bitmap = page.render(scale=min(2, 1600 / max(width, height)))
                                try:
                                    add_page(bitmap.to_pil(), text)
                                finally:
                                    bitmap.close()
                            finally:
                                page.close()
                    finally:
                        pdf.close()
                else:
                    require(raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff'),
                            '仅支持 PDF、PNG 和 JPEG')
                    with Image.open(io.BytesIO(raw)) as original:
                        require(original.format in ('PNG', 'JPEG'))
                        require(original.width * original.height <= 40_000_000, '图片像素过大')
                        original.load()
                        add_page(ImageOps.exif_transpose(original))
            draft['cover_page'] = 1 if draft['pages'] else None
            self._write(folder / 'draft.json', draft)
            return draft
        except Exception as exc:
            # Only our freshly created UUID child can be removed.
            require(folder.resolve().parent == self.root and not folder.is_symlink())
            shutil.rmtree(folder)
            if isinstance(exc, ValueError):
                raise
            raise ValueError('文件损坏、加密或无法读取') from exc

    def page_path(self, draft_id, number):
        draft = self.get(draft_id)
        require(type(number) is int and 1 <= number <= len(draft['pages']))
        path = self._folder(draft_id) / f'{number}.png'
        require(not path.is_symlink())
        return path

    def page_thumbnail(self, draft_id, number):
        """Read-only preview; never rewrite or crop the uploaded source page."""
        with Image.open(self.page_path(draft_id, number)) as image:
            image.thumbnail((160, 220))
            buffer = io.BytesIO()
            image.convert('RGB').save(buffer, 'JPEG', quality=80)
            return buffer.getvalue()

    @staticmethod
    def _revision(draft, revision):
        require(type(revision) is int and revision == draft['revision'], '草稿已更新，请刷新后重试')

    def _save(self, draft):
        draft['revision'] += 1
        self._write(self._folder(draft['id']) / 'draft.json', draft)
        return draft

    @staticmethod
    def _content(body, page_count, publishing=False):
        title = body.get('title', '')
        require(isinstance(title, str) and len(title) <= 160)
        require(not publishing or bool(title.strip()), '请填写教材名称')
        rows = body.get('lessons', [])
        require(isinstance(rows, list) and len(rows) <= 200)
        require(not publishing or bool(rows), '请先核对课文和字表')
        result, seen = [], set()
        for row in rows:
            require(isinstance(row, dict))
            clean = {}
            for key in ('number', 'unit'):
                value = row.get(key)
                require((value is None and not publishing) or (type(value) is int and 1 <= value <= 999),
                        '请填写有效的课号和单元号')
                clean[key] = value
            if clean['number'] is not None:
                identity = (clean['unit'], clean['number'])
                require(identity not in seen, '同一单元的课号不能重复')
                seen.add(identity)
            clean['title'] = row.get('title', '')
            require(isinstance(clean['title'], str) and len(clean['title']) <= 160)
            require(not publishing or bool(clean['title'].strip()), '请填写课文名称')
            for group in ('four', 'two'):
                chars = row.get(group, [])
                require(isinstance(chars, list) and len(chars) <= 200)
                require(all(isinstance(c, str) and HANZI.fullmatch(c) for c in chars), '字表必须逐字填写汉字')
                clean[group] = list(dict.fromkeys(chars))
            # Optional parent-entered material, never inferred from word lists.
            for key, limit in (('expansions', 10000), ('text', 100000)):
                value = row.get(key, '')
                require(isinstance(value, str) and len(value) <= limit,
                        '扩词或课文内容格式无效或过长')
                clean[key] = value
            pages = row.get('source_pages', [])
            require(isinstance(pages, list) and len(pages) <= MAX_PAGES and
                    all(type(p) is int and 1 <= p <= page_count for p in pages), '来源页码无效')
            clean['source_pages'] = list(dict.fromkeys(pages))
            require(type(row.get('reviewed', False)) is bool)
            clean['reviewed'] = row.get('reviewed', False)
            require(not publishing or clean['reviewed'], '请逐课核对后再保存教材')
            result.append(clean)
        return title.strip(), result

    def update(self, draft_id, body):
        with self._lock:
            draft = self.get(draft_id)
            self._revision(draft, body.get('revision'))
            require(draft['status'] not in ('recognizing', 'published'), '当前草稿不可修改')
            draft['title'], draft['lessons'] = self._content(body, len(draft['pages']))
            cover = body.get('cover_page', draft.get('cover_page'))
            require(cover is None or (type(cover) is int and 1 <= cover <= len(draft['pages'])),
                    '封面页码无效')
            draft['cover_page'] = cover
            units = body.get('page_units', [])
            require(isinstance(units, list) and len(units) <= len(draft['pages']), '书页关联无效')
            seen_pages = set()
            for item in units:
                require(isinstance(item, dict), '书页关联无效')
                number, unit = item.get('number'), item.get('unit')
                require(type(number) is int and 1 <= number <= len(draft['pages'])
                        and number not in seen_pages, '书页关联无效')
                require(unit is None or (type(unit) is int and 1 <= unit <= 999), '单元号无效')
                seen_pages.add(number)
                draft['pages'][number - 1]['unit'] = unit
            draft['status'] = 'review'
            draft['error'] = ''
            return self._save(draft)

    def read_book(self, book_id):
        """Management view, including legacy books without an import draft."""
        from lira.textbook_practice import TextbookPractice
        require(isinstance(book_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', book_id))
        book = next((b for b in TextbookPractice(self.books_dir).books() if b['id'] == book_id), None)
        if book is None:
            raise FileNotFoundError('未找到教材')
        pages, cover, revision = [], None, None
        if re.fullmatch(r'[a-f0-9]{32}', book_id):
            try:
                source = self.get(book_id)
                pages = source.get('pages', [])
                revision = source.get('revision')
                sources = {(row.get('unit'), row.get('number')):row.get('source_pages', [])
                           for row in source.get('lessons', [])}
                book['lessons'] = [dict(row, source_pages=sources.get((row['unit'], row['number']), []))
                                   for row in book['lessons']]
                cover = source.get('cover_page')
                if type(cover) is not int or not 1 <= cover <= len(pages):
                    cover = None
            except (OSError, ValueError):
                pass
        return dict(book, pages=pages, cover_page=cover, source_revision=revision)

    def set_book_cover(self, book_id, revision, cover_page):
        """Only presentation metadata changes; published lesson bytes stay fixed."""
        with self._lock:
            self.read_book(book_id)
            draft = self.get(book_id)
            self._revision(draft, revision)
            require(draft['status'] == 'published', '请在草稿中保存封面')
            require(cover_page is None or (type(cover_page) is int and
                    1 <= cover_page <= len(draft['pages'])), '封面页码无效')
            draft['cover_page'] = cover_page
            self._save(draft)
            return self.read_book(book_id)

    def revise_book(self, book_id):
        """Copy into a new review draft; published bytes remain immutable."""
        with self._lock:
            book = self.read_book(book_id)
            draft_id = uuid.uuid4().hex
            folder = self._folder(draft_id)
            folder.mkdir()
            try:
                pages = []
                for page in book['pages']:
                    number = page['number']
                    source = self.page_path(book_id, number)
                    shutil.copyfile(source, folder / f'{number}.png')
                    pages.append(dict(number=number, text=page.get('text', ''), unit=page.get('unit'),
                        image_url=f'/dev/api/textbooks/drafts/{draft_id}/pages/{number}'))
                lessons = [dict(row, reviewed=False, source_pages=row.get('source_pages', [])) for row in book['lessons']]
                title, lessons = self._content(dict(title=book['title'], lessons=lessons), len(pages))
                draft = dict(id=draft_id, status='review', revision=1, imported_at=time.time(),
                    title=title + '（修订）' if len(title) <= 156 else title,
                    lessons=lessons, pages=pages, warnings=[], error='',
                    cover_page=book['cover_page'], source_book_id=book_id)
                self._write(folder / 'draft.json', draft)
                return draft
            except BaseException:
                require(folder.resolve().parent == self.root and not folder.is_symlink())
                shutil.rmtree(folder)
                raise

    def set_recognizing(self, draft_id):
        with self._lock:
            draft = self.get(draft_id)
            require(draft['status'] not in ('recognizing', 'published'), '当前草稿不可重新识别')
            draft.update(status='recognizing', error='')
            return self._save(draft)

    def finish_recognition(self, draft_id, result):
        with self._lock:
            draft = self.get(draft_id)
            require(draft['status'] == 'recognizing')
            title, rows = self._content(result, len(draft['pages']))
            for row in rows:
                row['reviewed'] = False
            warnings = result.get('warnings', [])
            require(isinstance(warnings, list) and len(warnings) <= 200 and
                    all(isinstance(w, str) and len(w) <= 1000 for w in warnings))
            pages = result.get('pages', [])
            require(isinstance(pages, list) and len(pages) <= len(draft['pages']))
            seen_pages = set()
            for page in pages:
                require(isinstance(page, dict))
                number, text = page.get('number'), page.get('text')
                require(type(number) is int and 1 <= number <= len(draft['pages'])
                        and number not in seen_pages)
                require(isinstance(text, str) and len(text) <= 30000)
                seen_pages.add(number)
                # Only text is accepted from model output; image paths stay local.
                draft['pages'][number - 1]['text'] = text
            draft.update(title=title, lessons=rows, warnings=warnings, status='review', error='')
            return self._save(draft)

    def update_page_text(self, draft_id, number, text):
        """Persist OCR even if subsequent structured recognition fails."""
        with self._lock:
            draft = self.get(draft_id)
            require(draft['status'] == 'recognizing')
            require(type(number) is int and 1 <= number <= len(draft['pages']))
            require(isinstance(text, str) and len(text) <= 30000)
            draft['pages'][number - 1]['text'] = text
            return self._save(draft)

    def fail_recognition(self, draft_id, error):
        with self._lock:
            draft = self.get(draft_id)
            require(draft['status'] == 'recognizing')
            draft.update(status='failed', error=str(error)[:1000])
            return self._save(draft)

    def publish(self, draft_id, revision, confirmed):
        with self._lock:
            require(confirmed is True, '需要明确确认保存教材')
            draft = self.get(draft_id)
            if draft['status'] != 'published':
                self._revision(draft, revision)
                require(draft['status'] == 'review', '请先核对教材')
            title, lessons = self._content(draft, len(draft['pages']), publishing=True)
            book = dict(id=draft_id, title=title, lessons=[
                {k: row[k] for k in ('number', 'unit', 'title', 'four', 'two')} |
                {k: row[k] for k in ('expansions', 'text') if row[k]}
                for row in lessons])
            raw = json.dumps(book, ensure_ascii=False, indent=2).encode('utf-8')
            require(len(raw) <= 1024 * 1024, '教材字表过大')
            target = self.books_dir / f'{draft_id}.json'
            require(not target.is_symlink())
            if target.exists():
                require(target.read_bytes() == raw,
                        '教材已存在，禁止覆盖')
                if draft['status'] != 'published':
                    draft['status'] = 'published'
                    self._save(draft)
                return book
            require(draft['status'] != 'published', '已保存的教材文件缺失')
            # Hard-link a completed temporary file: atomic visibility without overwrite.
            temp = self.books_dir / ('.' + draft_id + '.' + uuid.uuid4().hex)
            try:
                with temp.open('xb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            draft['status'] = 'published'
            self._save(draft)
            return book
