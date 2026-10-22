"""Read-only contract for future textbook imports; never invent lesson words."""
from __future__ import annotations

import json
from pathlib import Path
import re


def needs_input(fields, question):
    return {'status':'needs_input','missing_parameters':fields,'question':question}


def require(condition):
    if not condition:raise ValueError('Invalid textbook data')


class TextbookPractice:
    def __init__(self, directory=None, *, draft_directory=None):
        self.directory=Path(directory).resolve() if directory else None
        self.draft_directory = (Path(draft_directory).resolve() if draft_directory else
                                self.directory.parent / "textbook-imports" if self.directory else None)

    def books(self):
        if not self.directory or not self.directory.is_dir():return []
        books=[]
        self._published_times={}
        for path in sorted(self.directory.glob('*.json'))[:64]:
            if path.is_symlink():continue
            try:
                with path.open('rb') as stream:raw=stream.read(1024*1024+1)
                if len(raw)>1024*1024:continue
                book=json.loads(raw)
                require(isinstance(book,dict))
                require(isinstance(book['id'],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',book['id']))
                require(isinstance(book['title'],str) and 1<=len(book['title'])<=160)
                lessons=book['lessons']
                require(isinstance(lessons,list) and 1<=len(lessons)<=200)
                seen=set()
                for lesson in lessons:
                    require(isinstance(lesson,dict))
                    for key in ('number','unit'):
                        require(type(lesson[key]) is int and 1<=lesson[key]<=999)
                    identity=(lesson['unit'],lesson['number'])
                    require(identity not in seen)
                    seen.add(identity)
                    require(isinstance(lesson['title'],str) and len(lesson['title'])<=160)
                    for group in ('four','two'):
                        chars=lesson[group]
                        require(isinstance(chars,list) and len(chars)<=200)
                        require(all(isinstance(c,str) and re.fullmatch(r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ebef]',c) for c in chars))
                books.append(book)
                self._published_times[book["id"]]=path.stat().st_mtime
            except (OSError,ValueError,KeyError,TypeError):continue
        # Ambiguous IDs must never silently select one of two imported books.
        ids=[book['id'] for book in books]
        return [book for book in books if ids.count(book['id'])==1]

    def available_books(self):
        """Read reviewed draft lessons without publishing or mutating imports."""
        books = self.books()
        known = {b['id'] for b in books}
        root = self.draft_directory
        if root and root.is_dir():
            for folder in sorted(root.iterdir())[:256]:
                if folder.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', folder.name):
                    continue
                path = folder / 'draft.json'
                if path.is_symlink():
                    continue
                try:
                    with path.open('rb') as stream:
                        raw = stream.read(16 * 1024 * 1024 + 1)
                    require(len(raw) <= 16 * 1024 * 1024)
                    draft = json.loads(raw)
                    require(draft['id'] == folder.name)
                    if draft['id'] in known:
                        next(b for b in books if b['id']==draft['id'])['imported_at']=draft.get('imported_at',folder.stat().st_mtime)
                        continue
                    if draft['status'] != 'review':continue
                    require(isinstance(draft['title'], str) and 0 < len(draft['title'].strip()) <= 160)
                    rows = draft['lessons']
                    require(isinstance(rows, list) and len(rows) <= 200)
                    lessons = []
                    seen = set()
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if not all(type(row.get(k)) is int and 1 <= row[k] <= 999 for k in ('unit', 'number')):
                            continue
                        identity = (row['unit'], row['number'])
                        require(identity not in seen)
                        seen.add(identity)
                        require(isinstance(row.get('title'), str) and len(row['title']) <= 160)
                        require(type(row.get('reviewed', False)) is bool)
                        for group in ('four', 'two'):
                            chars = row.get(group)
                            require(isinstance(chars, list) and len(chars) <= 200)
                            require(all(isinstance(c, str) and re.fullmatch(r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ebef]', c) for c in chars))
                        lessons.append(row)
                    books.append(dict(id=draft['id'], title=draft['title'], subject=draft.get('subject','chinese'), lessons=lessons, revision=draft.get('revision',1),
                                      source='review_draft', imported_at=draft.get('imported_at',folder.stat().st_mtime)))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        for book in books:
            book.setdefault('source', 'published')
            if 'imported_at' not in book:
                book['imported_at'] = self._published_times.get(book['id'],0)
        return books

    def catalog(self, *, books=None, include_drafts=True):
        if books is None:
            books = (self.available_books() if include_drafts else
                     [dict(b, source='published', imported_at=self._published_times.get(b['id'],0)) for b in self.books()])
        return {'status': 'ok', 'books': [
            {'book_id': b['id'], 'title': b['title'], 'subject': b.get('subject', 'chinese'),
             'source': b['source'], 'imported_at': b['imported_at'],
             'lessons': [{'number': l['number'], 'unit': l['unit'], 'title': l['title'],
                          'reviewed': l.get('reviewed', b['source'] == 'published')}
                         for l in b['lessons']]}
            for b in books],
            'instructions': '只可使用 reviewed=true 的课次，无需整本发布。imported_at 是本地导入目录时间，旧教材可能仅有文件时间；多本选用必须语音确认。'}

    def prepare(self,args, *, books=None):
        books=self.available_books() if books is None else books
        if not books:
            return {'status':'textbook_required','reason':'尚无可用教材。只需保存核对要练习的课次，不必核对或发布整本；不能猜测生字。'}
        book_id=args.get('book_id') or (books[0]['id'] if len(books)==1 else '')
        book=next((b for b in books if b['id']==book_id),None)
        if book is None:
            return needs_input(['book_id'],'这次使用哪一本已导入的语文课本？请根据教材列表确认。')
        missing=[]
        for key in ('scope_type','start','character_group'):
            if not args.get(key):missing.append(key)
        if args.get('scope_type')=='lesson_range' and not args.get('end'):missing.append('end')
        if missing:
            questions={'scope_type':'这次练习哪一课、哪个单元，或第几课到第几课？',
                'start':'从第几课开始？如果按单元练习，是第几单元？',
                'end':'练习到第几课？','character_group':'练四会字、二会字，还是两类都练？'}
            return needs_input(missing,questions[missing[0]])
        scope=args['scope_type'];start=args['start'];end=args.get('end',start)
        if scope=='lesson_range' and end<start:
            return needs_input(['start','end'],'起始课不能晚于结束课，请确认从第几课到第几课。')
        if scope!='lesson_range' and 'end' in args:
            return needs_input(['scope_type','end'],'你要练一课、一个单元，还是连续几课？单课和单元不用结束课号。')
        lessons=sorted(book['lessons'],key=lambda l:(l['unit'],l['number']))
        if scope=='unit':selected=[l for l in lessons if l['unit']==start]
        else:
            selected=[l for l in lessons if start<=l['number']<=end]
            if args.get('unit') is not None:selected=[l for l in selected if l['unit']==args['unit']]
            if len({l['number'] for l in selected})!=len(selected):
                return needs_input(['unit'],'阅读课和识字课有重复课号，请确认是第几单元的课文。')
            if {l['number'] for l in selected}!=set(range(start,end+1)):selected=[]
        if not selected:
            return needs_input(['start']+(['end'] if scope=='lesson_range' else []),'教材中找不到完整的指定范围，请根据目录确认课号或单元号。')
        if any(not l.get('reviewed', book['source'] == 'published') for l in selected):
            return {'status':'lesson_review_required','reason':'指定课次尚未保存核对；其他课次无需全部核对。'}
        groups=('four','two') if args['character_group']=='all' else (args['character_group'],)
        chars=list(dict.fromkeys(c for lesson in selected for group in groups for c in lesson[group]))
        if not chars:
            return {'status':'no_characters','reason':'指定范围没有登记这类字，请确认教材字表和练习类别；没有准备展示。'}
        text='\n'.join('　'.join(chars[i:i+4]) for i in range(0,len(chars),4))
        if len(text)>8000:
            return needs_input(['start','end'],'本次范围过大，请缩小练习范围。')
        return {'status':'ready','text':text,'book_id':book['id'],'book_title':book['title'],
            'lesson_numbers':[l['number'] for l in selected],'character_group':args['character_group'],'character_count':len(chars)}
