"""Owner-scoped review revisions and managed photos for frozen v2 tasks.

Callers authenticate the parent and serialize mutations. SQLite owns facts; files
left by interrupted or rolled-back outer transactions can be reclaimed explicitly.
"""
import hashlib
import io
import json
from pathlib import Path
import uuid

from .db_atomic import atomic, revision


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def checked_text(value, limit=4000):
    if type(value) is not str or len(value) > limit:
        raise ValueError('invalid text')
    return value


class DictationResults:
    MAX_BYTES = 10 * 1024 * 1024

    def __init__(self, db, root, repository, member_exists=None):
        self.db, self.root, self.repository = db, Path(root).resolve(), repository
        self.member_exists = member_exists or (lambda owner: bool(db.execute('SELECT 1 FROM members WHERE id=?', (owner,)).fetchone()))
        self.root.mkdir(parents=True, exist_ok=True)
        for sql in (
            'CREATE TABLE IF NOT EXISTS dictation_result_revisions(task_id TEXT, revision INTEGER, owner_id INTEGER, request_id TEXT, fingerprint TEXT, payload TEXT, PRIMARY KEY(task_id,revision), UNIQUE(task_id,request_id))',
            'CREATE TABLE IF NOT EXISTS dictation_result_facts(task_id TEXT PRIMARY KEY, owner_id INTEGER, revision INTEGER, payload TEXT)',
            'CREATE TABLE IF NOT EXISTS dictation_result_photos(attachment_id TEXT PRIMARY KEY, task_id TEXT, owner_id INTEGER, filename TEXT, content_type TEXT, size INTEGER, sha256 TEXT)',
            'CREATE TABLE IF NOT EXISTS dictation_result_assistance(task_id TEXT, item_id TEXT, owner_id INTEGER, source_turn TEXT, text TEXT, PRIMARY KEY(task_id,item_id,source_turn))',
        ):
            db.execute(sql)

    def _member(self, owner):
        if type(owner) is not int or owner < 1 or not self.member_exists(owner):
            raise ValueError('invalid member')

    def _task(self, owner, task_id):
        self._member(owner)
        row = self.repository._task(task_id)
        if row['owner_id'] != owner or row['revoked']:
            raise ValueError('unauthorized or revoked task')
        snapshot = self.repository.snapshot(task_id)
        if not snapshot or snapshot.get('schema_version') != 2:
            raise ValueError('v2 task required')
        return snapshot

    def get(self, owner_id, task_id):
        self._task(owner_id, task_id)
        row = self.db.execute('SELECT payload FROM dictation_result_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1', (task_id,)).fetchone()
        return json.loads(row[0]) if row else dict(task_id=task_id, revision=0, status='unreviewed', errors=[], recognition_errors=[], note='')

    def review(self, owner_id, task_id, *, status, errors, recognition_errors=None, note='', expected_revision, request_id):
        snapshot = self._task(owner_id, task_id)
        revision(expected_revision)
        if not checked_text(request_id, 128): raise ValueError('request id required')
        checked_text(note)
        recognition_errors = [] if recognition_errors is None else recognition_errors
        if type(errors) is not list or type(recognition_errors) is not list:
            raise ValueError('invalid errors')
        items = {i['item_id']:i for p in snapshot['phases'] if p['type']=='dictation' for i in p['items']}
        normalized, seen = [], set()
        for error in errors:
            if type(error) is not dict or set(error)-{'item_id','wrong_characters','note'}:
                raise ValueError('invalid error')
            item = items.get(error.get('item_id'))
            if not item or item['item_id'] in seen: raise ValueError('invalid error item')
            seen.add(item['item_id'])
            chars = error.get('wrong_characters', [])
            if type(chars) is not list or any(type(c) is not str or len(c)!=1 or c not in item['text'] for c in chars) or len(set(chars))!=len(chars):
                raise ValueError('invalid wrong characters')
            normalized.append(dict(item_id=item['item_id'], text=item['text'], lesson_id=item['lesson_id'], wrong_characters=chars, note=checked_text(error.get('note',''))))
        targets = {c for lesson in snapshot['lessons'] for c in lesson['two_targets']}
        if any(type(c) is not str or c not in targets for c in recognition_errors) or len(set(recognition_errors)) != len(recognition_errors):
            raise ValueError('invalid recognition target')
        if status not in ('unreviewed','reviewed_with_errors','reviewed_all_correct') or (status=='reviewed_with_errors') != bool(normalized or recognition_errors):
            raise ValueError('review status conflicts with errors')
        payload = dict(task_id=task_id, revision=expected_revision+1, status=status, errors=normalized, recognition_errors=recognition_errors, note=note)
        raw = encoded(payload)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        with atomic(self.db):
            self._task(owner_id, task_id)
            previous = self.db.execute('SELECT fingerprint,payload FROM dictation_result_revisions WHERE task_id=? AND request_id=?', (task_id, request_id)).fetchone()
            if previous:
                if previous[0] != fingerprint: raise ValueError('request id conflict')
                return json.loads(previous[1])
            if self.get(owner_id,task_id)['revision'] != expected_revision: raise ValueError('revision: conflict')
            self.db.execute('INSERT INTO dictation_result_revisions VALUES(?,?,?,?,?,?)', (task_id, expected_revision+1, owner_id, request_id, fingerprint, raw))
            self.db.execute('INSERT OR REPLACE INTO dictation_result_facts VALUES(?,?,?,?)', (task_id, owner_id, expected_revision+1, raw))
        return payload

    def photos(self, owner_id, task_id):
        self._task(owner_id, task_id)
        return [dict(zip(('attachment_id','task_id','owner_id','content_type','size','sha256'), row)) for row in self.db.execute('SELECT attachment_id,task_id,owner_id,content_type,size,sha256 FROM dictation_result_photos WHERE task_id=? ORDER BY rowid', (task_id,))]

    def _photo(self, owner, attachment_id):
        row = self.db.execute('SELECT task_id,filename FROM dictation_result_photos WHERE attachment_id=? AND owner_id=?', (attachment_id, owner)).fetchone()
        if not row: raise ValueError('unknown attachment')
        self._task(owner,row[0])
        path = (self.root / row[1]).resolve()
        if path.parent != self.root: raise ValueError('invalid attachment path')
        return row[0], path

    def upload(self, owner_id, task_id, data, content_type='image/png', replace_id=None):
        self._task(owner_id, task_id)
        if type(data) is not bytes or not 0 < len(data) <= self.MAX_BYTES or content_type not in ('image/png','image/jpeg'):
            raise ValueError('invalid image')
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.format != {'image/png':'PNG','image/jpeg':'JPEG'}[content_type] or im.width*im.height > 25000000:
                    raise ValueError('invalid image format or dimensions')
                im.verify()
        except Exception as exc:
            raise ValueError('invalid image') from exc
        attachment_id = uuid.uuid4().hex
        final = self.root / (attachment_id + ('.png' if content_type=='image/png' else '.jpg'))
        temporary = self.root / (attachment_id + '.tmp')
        old_path = None
        try:
            temporary.write_bytes(data)
            temporary.replace(final)
            with atomic(self.db):
                self._task(owner_id,task_id)
                if replace_id:
                    old_task, old_path = self._photo(owner_id, replace_id)
                    if old_task != task_id: raise ValueError('replacement belongs to another task')
                self.db.execute('INSERT INTO dictation_result_photos VALUES(?,?,?,?,?,?,?)', (attachment_id,task_id,owner_id,final.name,content_type,len(data),hashlib.sha256(data).hexdigest()))
                if replace_id: self.db.execute('DELETE FROM dictation_result_photos WHERE attachment_id=?', (replace_id,))
        except BaseException:
            temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise
        # Old files remain recoverable until the connection owner commits; reclaim
        # only outside a transaction, so an outer rollback cannot destroy a photo.
        if old_path and not self.db.in_transaction: old_path.unlink(missing_ok=True)
        return next(p for p in self.photos(owner_id,task_id) if p['attachment_id']==attachment_id)

    def read_photo(self, owner_id, attachment_id):
        task_id, path = self._photo(owner_id,attachment_id)
        metadata = next(p for p in self.photos(owner_id,task_id) if p['attachment_id']==attachment_id)
        return dict(metadata, data=path.read_bytes())

    def delete_photo(self, owner_id, attachment_id):
        with atomic(self.db):
            _, path = self._photo(owner_id,attachment_id)
            self.db.execute('DELETE FROM dictation_result_photos WHERE attachment_id=?', (attachment_id,))
        if not self.db.in_transaction: path.unlink(missing_ok=True)

    def reclaim_files(self):
        """Run after commits, while the caller's mutation lock is held."""
        if self.db.in_transaction: raise ValueError('commit before reclaiming files')
        referenced = {r[0] for r in self.db.execute('SELECT filename FROM dictation_result_photos')}
        for path in self.root.iterdir():
            if path.is_file() and path.name not in referenced: path.unlink()

    def record_assistance(self, owner_id, task_id, item_id, text, source_turn):
        snapshot = self._task(owner_id,task_id)
        if item_id not in {i['item_id'] for p in snapshot['phases'] for i in p['items']}:
            raise ValueError('unknown assistance item')
        checked_text(text)
        if not checked_text(source_turn,128): raise ValueError('source turn required')
        with atomic(self.db):
            self._task(owner_id,task_id)
            old = self.db.execute('SELECT text FROM dictation_result_assistance WHERE task_id=? AND item_id=? AND source_turn=?',(task_id,item_id,source_turn)).fetchone()
            if old and old[0]!=text: raise ValueError('assistance conflict')
            self.db.execute('INSERT OR IGNORE INTO dictation_result_assistance VALUES(?,?,?,?,?)',(task_id,item_id,owner_id,source_turn,text))

    def facts(self, owner_id, book_id, unit, number):
        self._member(owner_id)
        result = dict(reviews=[], assistance=[], execution=[])
        for row in self.db.execute('SELECT task_id FROM dictation_tasks WHERE owner_id=? AND revoked=0 ORDER BY rowid', (owner_id,)):
            task_id = row[0]
            raw = self.repository.snapshot(task_id)
            if not raw or raw.get('schema_version') != 2: continue
            snapshot = self._task(owner_id,task_id)
            lessons = {l['lesson_id'] for l in snapshot['lessons'] if (l['source']['book_id'],l['source']['unit'],l['source']['number'])==(book_id,unit,number)}
            if not lessons: continue
            items = {i['item_id'] for p in snapshot['phases'] for i in p['items'] if i['lesson_id'] in lessons}
            review = self.get(owner_id,task_id)
            if review['revision']:
                review['errors'] = [e for e in review['errors'] if e['lesson_id'] in lessons]
                targets = {c for l in snapshot['lessons'] if l['lesson_id'] in lessons for c in l['two_targets']}
                review['recognition_errors'] = [c for c in review['recognition_errors'] if c in targets]
                result['reviews'].append(review)
            result['assistance'].extend(dict(task_id=task_id,item_id=r[0],source_turn=r[1],text=r[2]) for r in self.db.execute('SELECT item_id,source_turn,text FROM dictation_result_assistance WHERE task_id=?', (task_id,)) if r[0] in items)
            result['execution'].extend(e for e in self.repository.events(task_id) if e.get('item_id') in items or e.get('item_id') is None)
        return result

    def forget(self, owner_id):
        self._member(owner_id)
        with atomic(self.db):
            self.repository.revoke_owner(owner_id)
            for table in ('dictation_result_revisions','dictation_result_facts','dictation_result_photos','dictation_result_assistance'):
                self.db.execute('DELETE FROM '+table+' WHERE owner_id=?',(owner_id,))
        if not self.db.in_transaction: self.reclaim_files()
