import sqlite3
import hashlib
import pytest
from lira.teaching_skills import TeachingSkills

def test_seed_versions_conflict_and_outer_rollback():
    db = sqlite3.connect(':memory:')
    skills = TeachingSkills(db)
    seed = skills.get()
    assert seed['sha256'] == hashlib.sha256(seed['content'].encode('utf-8')).hexdigest()
    edited = skills.save('家长修订', expected_revision=seed['revision'])
    assert TeachingSkills(db).get() == edited
    assert skills.get(seed['revision']) == seed
    with pytest.raises(ValueError, match='conflict'):
        skills.save('迟到写入', expected_revision=seed['revision'])
    db.execute('BEGIN')
    skills.save('回滚', expected_revision=edited['revision'])
    db.rollback()
    assert skills.get() == edited

def test_constructor_never_commits_outer_transaction():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE sentinel (value TEXT)')
    db.execute("INSERT INTO sentinel VALUES ('pending')")
    TeachingSkills(db)
    assert db.in_transaction
    db.rollback()
    assert db.execute('SELECT * FROM sentinel').fetchall() == []

@pytest.mark.parametrize('content', ['', '   ', None, 'a'*100001], ids=['empty','blank','null','oversized'])
def test_invalid_skill(content):
    skills = TeachingSkills(sqlite3.connect(':memory:'))
    with pytest.raises(ValueError):
        skills.save(content, expected_revision=1)
