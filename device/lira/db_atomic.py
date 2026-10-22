"""Short SQLite units that preserve transactions owned by callers."""
from contextlib import contextmanager
import uuid

@contextmanager
def atomic(db):
    name = 'lira_' + uuid.uuid4().hex
    db.execute('SAVEPOINT ' + name)
    try:
        yield
        db.execute('RELEASE SAVEPOINT ' + name)
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT ' + name)
        db.execute('RELEASE SAVEPOINT ' + name)
        raise

def revision(value):
    if type(value) is not int or value < 0:
        raise ValueError('revision: invalid revision')
