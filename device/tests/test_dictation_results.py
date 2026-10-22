import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from lira.dictation_repository import DictationRepository
from lira.dictation_results import DictationResults


@pytest.fixture
def env(tmp_path):
    db = sqlite3.connect(tmp_path / 'db')
    db.execute('CREATE TABLE members(id INTEGER PRIMARY KEY)')
    db.execute('INSERT INTO members VALUES(1)')
    repo = DictationRepository(db)
    task = json.loads((Path(__file__).parents[2] / 'tv-receiver/tests/fixtures/dictation-v2.json').read_text(encoding='utf-8'))
    repo.submit(task, paired_device_id='tv', owner_id=1)
    db.commit()
    return db, repo, DictationResults(db, tmp_path / 'photos', repo), task


def review(service, **kw):
    return service.review(1, 'task-v2', status='reviewed_with_errors', errors=[{'item_id':'dictate-1','wrong_characters':['晴'],'note':'笔画'}], expected_revision=0, request_id='r1', **kw)


def png():
    out = io.BytesIO()
    Image.new('RGB', (2, 2)).save(out, format='PNG')
    return out.getvalue()


def test_review_latest_idempotency_and_correction(env):
    db, repo, s, task = env
    assert s.get(1, task['task_id'])['status'] == 'unreviewed'
    assert review(s) == review(s)
    assert s.facts(1, 'book-1', 1, 1)['reviews'][0]['errors'][0]['text'] == '晴天'
    with pytest.raises(ValueError, match='revision'):
        s.review(1, 'task-v2', status='reviewed_all_correct', errors=[], expected_revision=0, request_id='r2')
    s.review(1, 'task-v2', status='reviewed_all_correct', errors=[], expected_revision=1, request_id='r2')
    facts = s.facts(1, 'book-1', 1, 1)
    assert len(facts['reviews']) == 1 and facts['reviews'][0]['errors'] == []
    with pytest.raises(ValueError):
        review(s, note='different')
    db.commit()
    assert DictationResults(db, s.root, repo).get(1, 'task-v2')['revision'] == 2


@pytest.mark.parametrize('status,errors,recognition', [
    ('reviewed_with_errors', [], []), ('reviewed_all_correct', [{'item_id':'dictate-1'}], []),
    ('unreviewed', [], ['清']), ('reviewed_with_errors',[{'item_id':'dictate-1','wrong_characters':['错']}], []),
    ('reviewed_with_errors', [], ['晴']), ('reviewed_with_errors',[{'item_id':'recognize-1'}], []),
])
def test_invalid_review_is_atomic(env, status, errors, recognition):
    db, repo, s, task = env
    with pytest.raises(ValueError):
        s.review(1, 'task-v2', status=status, errors=errors, recognition_errors=recognition, expected_revision=0, request_id='bad')
    assert s.get(1, 'task-v2')['revision'] == 0


def test_photo_roundtrip_replace_failure_and_forget(env):
    db, repo, s, task = env
    photo = s.upload(1, 'task-v2', png())
    assert s.read_photo(1, photo['attachment_id'])['data'] == png()
    assert s.get(1, 'task-v2')['status'] == 'unreviewed'
    with pytest.raises(ValueError): s.upload(1, 'task-v2', b'\x89PNG\r\n\x1a\nnot an image')
    db.execute("CREATE TRIGGER fail_photo BEFORE INSERT ON dictation_result_photos BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): s.upload(1, 'task-v2', png(), replace_id=photo['attachment_id'])
    assert len(s.photos(1, 'task-v2')) == 1
    assert len(list(s.root.glob('*.png'))) == 1
    db.execute('DROP TRIGGER fail_photo')
    replacement = s.upload(1, 'task-v2', png(), replace_id=photo['attachment_id'])
    assert replacement['attachment_id'] != photo['attachment_id']
    s.record_assistance(1, 'task-v2', 'dictate-1', '解释', 'turn-1')
    assert s.facts(1, 'book-1', 1, 1)['assistance'][0]['text'] == '解释'
    s.forget(1)
    assert not list(s.root.glob('*.png'))
    with pytest.raises(ValueError): review(s)
    with pytest.raises(ValueError): s.upload(1, 'task-v2', png())


def test_permissions_and_review_projection_rollback(env):
    db, repo, s, task = env
    with pytest.raises(ValueError): s.get(2, 'task-v2')
    db.execute("CREATE TRIGGER fail_fact BEFORE INSERT ON dictation_result_facts BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): review(s)
    assert s.get(1, 'task-v2')['revision'] == 0
    assert db.execute('SELECT count(*) FROM dictation_result_revisions').fetchone()[0] == 0


def test_reopen_and_two_connection_revision_conflict(env):
    db, repo, s, task = env
    path = db.execute('PRAGMA database_list').fetchone()[2]
    other_db = sqlite3.connect(path)
    other = DictationResults(other_db, s.root, DictationRepository(other_db))
    assert other.get(1, 'task-v2')['revision'] == 0
    review(s)
    with pytest.raises(ValueError, match='revision'):
        other.review(1, 'task-v2', status='reviewed_all_correct', errors=[], expected_revision=0, request_id='concurrent')
    other_db.close()
    db.close()
    reopened = sqlite3.connect(path)
    persisted = DictationResults(reopened, s.root, DictationRepository(reopened))
    assert persisted.facts(1, 'book-1', 1, 1)['reviews'][0]['revision'] == 1
    reopened.close()


def test_outer_rollback_preserves_original_photo_and_reclaims_orphan(env):
    db, repo, s, task = env
    original = s.upload(1, 'task-v2', png())
    db.execute('BEGIN')
    replacement = s.upload(1, 'task-v2', png(), replace_id=original['attachment_id'])
    with pytest.raises(ValueError, match='commit'):
        s.reclaim_files()
    db.rollback()
    assert s.read_photo(1, original['attachment_id'])['data'] == png()
    s.reclaim_files()
    assert len(list(s.root.glob('*.png'))) == 1
    with pytest.raises(ValueError): s.read_photo(2, original['attachment_id'])
    s.delete_photo(1, original['attachment_id'])
    assert s.photos(1, 'task-v2') == []
    assert not list(s.root.glob('*.png'))
