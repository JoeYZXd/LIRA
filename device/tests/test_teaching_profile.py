import sqlite3
import pytest
from lira.teaching_profile import TeachingProfile

def test_profiles_isolate_members_and_preserve_revisions():
    profile = TeachingProfile(sqlite3.connect(':memory:'), member_exists=lambda member:member in (1,2))
    assert profile.get(1) is None
    saved = profile.save(1,'book-a',{'unit':1,'lesson':2},expected_revision=0)
    assert saved == profile.get(1)
    assert profile.get(2) is None
    with pytest.raises(ValueError, match='conflict'):
        profile.save(1,'book-b',{},expected_revision=0)
    with pytest.raises(ValueError):
        profile.save(3,'book-a',{},expected_revision=0)

def test_members_table_validation_and_transaction_ownership():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE members (id INTEGER PRIMARY KEY)')
    profile = TeachingProfile(db)
    db.execute('INSERT INTO members VALUES (1)')
    profile.save(1,'book-a',{},expected_revision=0)
    assert db.in_transaction
    db.rollback()
    with pytest.raises(ValueError):
        profile.get(1)
    assert db.execute('SELECT * FROM teaching_profile_revisions').fetchall() == []

@pytest.mark.parametrize('progress', [{'mastery':True},{'unit':True},{'lesson':0},{'lesson':1000},[],None])
def test_progress_validation(progress):
    profile = TeachingProfile(sqlite3.connect(':memory:'),member_exists=lambda _:True)
    with pytest.raises(ValueError):
        profile.save(1,'book',progress,expected_revision=0)
