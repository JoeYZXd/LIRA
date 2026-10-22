import json,sqlite3
from pathlib import Path
import pytest
from lira.dictation_repository import DictationRepository

def task():return json.loads((Path(__file__).parents[2]/'tv-receiver/tests/fixtures/dictation-v1.json').read_text(encoding='utf-8'))
def ev(t,n,k,lesson=None):return dict(task_id=t['task_id'],event_id=str(n),run_epoch=1,sequence=n,state_revision=n,kind=k,lesson_id=lesson,content_hash=t['content_hash'])
def test_completion_atomic_and_forget(tmp_path):
 p=tmp_path/'db';db=sqlite3.connect(p);r=DictationRepository(db);t=task();r.submit(t,paired_device_id='tv',owner_id=7)
 r.receive(ev(t,2,'lesson_dictation_completed',t['lessons'][0]['lesson_id']),paired_device_id='tv');assert r.counts(7)==[]
 db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON dictation_word_history BEGIN SELECT RAISE(ABORT,'injected'); END")
 with pytest.raises(sqlite3.IntegrityError):r.receive(ev(t,1,'started'),paired_device_id='tv')
 assert not r.counts(7);assert db.execute('SELECT COUNT(*) FROM dictation_event_inbox').fetchone()[0]==1
 db.execute('DROP TRIGGER fail_history');r.receive(ev(t,1,'started'),paired_device_id='tv');assert r.counts(7)[0]['count']==1
 r.receive(ev(t,3,'lesson_dictation_completed',t['lessons'][0]['lesson_id']),paired_device_id='tv');assert r.counts(7)[0]['count']==1
 r.revoke_owner(7);db.close();r=DictationRepository(sqlite3.connect(p));assert not r.counts(7)
 assert r.receive(ev(t,4,'cancelled'),paired_device_id='tv')['status']=='revoked'

def test_partial_unit_and_stable_revision(tmp_path):
 from lira.dictation_task_contract import content_hash
 db=sqlite3.connect(tmp_path/'db');r=DictationRepository(db);t=task()
 b=json.loads(json.dumps(t['lessons'][0]));b['lesson_id']='second';b['source']['number']=8;t['lessons'].append(b);t['content_hash']=content_hash(t)
 r.submit(t,paired_device_id='tv',owner_id=2);r.receive(ev(t,1,'started'),paired_device_id='tv');r.receive(ev(t,2,'lesson_dictation_completed',t['lessons'][0]['lesson_id']),paired_device_id='tv')
 r.request_cancel(t['task_id']);assert r.status(t['task_id'])['cancel_requested']==1
 r.receive(ev(t,3,'cancelled'),paired_device_id='tv');r.receive(ev(t,4,'lesson_dictation_completed','second'),paired_device_id='tv')
 assert r.counts(2)==[dict(book_id='book-1',unit=4,number=7,count=1)]
 t['task_id']='new';t['lessons']=t['lessons'][:1];t['lessons'][0]['source']['revision']=9;t['content_hash']=content_hash(t)
 r.submit(t,paired_device_id='tv',owner_id=2);r.receive(ev(t,1,'started'),paired_device_id='tv');r.receive(ev(t,2,'lesson_dictation_completed',t['lessons'][0]['lesson_id']),paired_device_id='tv')
 assert r.counts(2)[0]['count']==2
 assert len(r.word_history(2,'book-1',4,7))==2

def test_authority_conflicts_and_anonymous():
 from lira.dictation_repository import RepositoryError
 r=DictationRepository(sqlite3.connect(':memory:'));t=task();r.submit(t,paired_device_id='tv')
 e=ev(t,1,'started')
 with pytest.raises(RepositoryError):r.receive(e,paired_device_id='other')
 r.receive(e,paired_device_id='tv');r.receive(e,paired_device_id='tv')
 e['kind']='progress'
 with pytest.raises(RepositoryError):r.receive(e,paired_device_id='tv')
 e=ev(t,2,'lesson_dictation_completed','unknown')
 with pytest.raises(RepositoryError):r.receive(e,paired_device_id='tv')
 r.receive(ev(t,2,'lesson_dictation_completed',t['lessons'][0]['lesson_id']),paired_device_id='tv')
 assert r.counts(None)[0]['count']==1
 assert r.counts(2)==[]
 assert r.word_history(None,'book-1',4,7)==[]
 with pytest.raises(RepositoryError):r.submit(t,paired_device_id='tv',owner_id=2)

def test_restart_pending_and_event_id_conflict(tmp_path):
 from lira.dictation_repository import RepositoryError
 p=tmp_path/'db';db=sqlite3.connect(p);r=DictationRepository(db);t=task();r.submit(t,paired_device_id='tv',owner_id=2)
 r.receive(ev(t,2,'lesson_dictation_completed',t['lessons'][0]['lesson_id']),paired_device_id='tv');db.close()
 r=DictationRepository(sqlite3.connect(p));r.receive(ev(t,1,'started'),paired_device_id='tv');assert r.counts(2)[0]['count']==1
 e=ev(t,3,'progress');e['event_id']='2'
 with pytest.raises(RepositoryError):r.receive(e,paired_device_id='tv')
 assert r.status(t['task_id'])['last_sequence']==2

def test_cross_lesson_completion_need_not_follow_book_order():
 from lira.dictation_task_contract import content_hash
 t=task();b=json.loads(json.dumps(t['lessons'][0]));b['lesson_id']='four-only';b['source']['number']=8;b['two_targets']=[];b['recognition_words']=[];t['lessons'].append(b);t['content_hash']=content_hash(t)
 r=DictationRepository(sqlite3.connect(':memory:'));r.submit(t,paired_device_id='tv',owner_id=1);r.receive(ev(t,1,'started'),paired_device_id='tv');r.receive(ev(t,2,'lesson_dictation_completed','four-only'),paired_device_id='tv')
 assert r.counts(1)==[dict(book_id='book-1',unit=4,number=8,count=1)]
 r.receive(ev(t,3,'lesson_dictation_completed',t['lessons'][0]['lesson_id']),paired_device_id='tv');assert len(r.counts(1))==2

def test_repository_constructor_preserves_outer_transaction():
 db=sqlite3.connect(':memory:');db.execute('CREATE TABLE sentinel(value)');db.execute('INSERT INTO sentinel VALUES(1)')
 DictationRepository(db);db.rollback()
 assert db.execute('SELECT * FROM sentinel').fetchall()==[]

def v2_event(t,n,action='task_received',state='waiting_start',**extra):
 return dict(schema_version=2,task_id=t['task_id'],content_hash=t['content_hash'],event_id='event-'+str(n),run_epoch=1,sequence=n,state_revision=n,command_id=None,phase_id=None,item_id=None,action=action,state=state,actual_repeats=0,boot_id='boot',session_id='session',occurred_at='2026-10-21T00:00:00Z',**extra)

def test_v2_ordered_actual_state_and_commands():
 t=json.loads((Path(__file__).parents[2]/'tv-receiver/tests/fixtures/dictation-v2.json').read_text(encoding='utf-8'))
 db=sqlite3.connect(':memory:');r=DictationRepository(db);r.submit(t,paired_device_id='tv',owner_id=1)
 assert r.status(t['task_id'])['state']=='ready'
 assert r.receive(v2_event(t,2,'paused','paused'),paired_device_id='tv')['last_sequence']==0
 r.receive(v2_event(t,1),paired_device_id='tv');assert r.status(t['task_id'])['state']=='paused'
 cmd=dict(schema_version=2,command_id='cmd-1',task_id=t['task_id'],content_hash=t['content_hash'],run_epoch=1,expected_state_revision=2,phase_id=None,item_id=None,action='resume')
 assert r.request_command(cmd)['status']=='pending'
 assert r.request_command(cmd)['status']=='pending'
 assert r.status(t['task_id'])['state']=='paused'
 assert r.snapshot(t['task_id'])==t
 r.receive(v2_event(t,3,'resumed','waiting_child')|{'command_id':'cmd-1'},paired_device_id='tv')
 assert r.request_command(cmd)['status']=='applied'
 assert len(r.events(t['task_id']))==3

def test_v2_rejection_identity_epoch_terminal_and_outer_rollback():
 from lira.dictation_repository import RepositoryError
 t=json.loads((Path(__file__).parents[2]/'tv-receiver/tests/fixtures/dictation-v2.json').read_text(encoding='utf-8'))
 db=sqlite3.connect(':memory:');r=DictationRepository(db);r.submit(t,paired_device_id='tv')
 with pytest.raises(RepositoryError):r.receive(v2_event(t,1),paired_device_id='other')
 with pytest.raises(RepositoryError):r.receive(v2_event(t,1)|{'run_epoch':2},paired_device_id='tv')
 e=v2_event(t,1);r.receive(e,paired_device_id='tv');r.receive(e,paired_device_id='tv')
 with pytest.raises(RepositoryError):r.receive(e|{'state':'paused'},paired_device_id='tv')
 r.receive(v2_event(t,2,'cancelled','cancelled'),paired_device_id='tv')
 r.receive(v2_event(t,3,'resumed','playing'),paired_device_id='tv');assert r.status(t['task_id'])['state']=='cancelled'
 db.execute('BEGIN');r.submit(t|{'task_id':'other','content_hash':__import__('lira.dictation_task_contract',fromlist=['content_hash']).content_hash(t|{'task_id':'other'})},paired_device_id='tv');db.rollback()
 with pytest.raises(RepositoryError):r.status('other')
