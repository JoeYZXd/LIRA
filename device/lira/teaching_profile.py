"""Child textbook bindings. Management callers own authorization and book confirmation."""
import json
from .db_atomic import atomic, revision

class TeachingProfile:
    def __init__(self, connection, member_exists=None):
        self.db = connection
        self.member_exists = member_exists or self._stored_member_exists
        self.db.execute('CREATE TABLE IF NOT EXISTS teaching_profile_revisions (member_id INTEGER NOT NULL, revision INTEGER NOT NULL, book_id TEXT NOT NULL, progress TEXT NOT NULL, PRIMARY KEY(member_id,revision))')

    def _stored_member_exists(self, member_id):
        return bool(self.db.execute('SELECT 1 FROM members WHERE id=?', (member_id,)).fetchone())

    def _member(self, member_id):
        if type(member_id) is not int or member_id < 1 or not self.member_exists(member_id):
            raise ValueError('invalid member')

    def get(self, member_id):
        self._member(member_id)
        row = self.db.execute('SELECT revision,book_id,progress FROM teaching_profile_revisions WHERE member_id=? ORDER BY revision DESC LIMIT 1', (member_id,)).fetchone()
        return None if row is None else {'member_id':member_id,'revision':row[0],'book_id':row[1],'progress':json.loads(row[2])}

    def save(self, member_id, book_id, progress, expected_revision):
        self._member(member_id)
        revision(expected_revision)
        if not isinstance(book_id,str) or not book_id.strip() or len(book_id)>100:
            raise ValueError('invalid book id')
        if type(progress) is not dict or set(progress)-{'unit','lesson'}:
            raise ValueError('invalid progress')
        if any(type(n) is not int or not 1 <= n <= 999 for n in progress.values()):
            raise ValueError('invalid progress')
        encoded = json.dumps(progress,ensure_ascii=False,sort_keys=True)
        with atomic(self.db):
            self._member(member_id)
            latest = self.get(member_id)
            if (latest['revision'] if latest else 0) != expected_revision:
                raise ValueError('revision: conflict')
            self.db.execute('INSERT INTO teaching_profile_revisions VALUES (?,?,?,?)', (member_id,expected_revision+1,book_id,encoded))
        return self.get(member_id)
