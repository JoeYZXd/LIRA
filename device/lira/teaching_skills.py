"""Immutable business instructions; writes require a trusted management caller."""
import hashlib
from importlib.resources import files
from .db_atomic import atomic, revision

class TeachingSkills:
    def __init__(self, connection):
        self.db = connection
        with atomic(self.db):
            self.db.execute('CREATE TABLE IF NOT EXISTS teaching_skill_revisions (revision INTEGER PRIMARY KEY, content TEXT NOT NULL, sha256 TEXT NOT NULL)')
            if self.get() is None:
                self.save(files('lira.llm').joinpath('chinese_dictation_skill.md').read_text(encoding='utf-8'), expected_revision=0)

    def get(self, revision=None):
        if revision is None:
            row = self.db.execute('SELECT revision,content,sha256 FROM teaching_skill_revisions ORDER BY revision DESC LIMIT 1').fetchone()
        else:
            if type(revision) is not int or revision < 1:
                raise ValueError('invalid revision')
            row = self.db.execute('SELECT revision,content,sha256 FROM teaching_skill_revisions WHERE revision=?', (revision,)).fetchone()
        return None if row is None else dict(zip(('revision','content','sha256'), row))

    def save(self, content, expected_revision):
        revision(expected_revision)
        if not isinstance(content, str) or not content.strip() or len(content) > 100000:
            raise ValueError('invalid skill content')
        with atomic(self.db):
            latest = self.get()
            if (latest['revision'] if latest else 0) != expected_revision:
                raise ValueError('revision: conflict')
            self.db.execute('INSERT INTO teaching_skill_revisions VALUES (?,?,?)', (expected_revision+1, content, hashlib.sha256(content.encode('utf-8')).hexdigest()))
        return self.get(expected_revision+1)
