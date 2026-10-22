"""Cross-boundary lifecycle regression; no cloud or household device I/O."""
import pytest
from test_dictation_service import service


def test_forget_allows_trusted_new_session_but_never_old_session(service):
    scope = dict(scope_type='lesson', start=2)
    service.get_context(scope, owner_id=1, session_id='old')
    service.forget(1)
    with pytest.raises(ValueError, match='forgotten'):
        service.get_context(scope, owner_id=1, session_id='untrusted-tool')
    service.begin_session(1, 'new', turn='parent-click')
    assert service.get_context(scope, owner_id=1, session_id='new')['status'] == 'ready'
    with pytest.raises(ValueError, match='forgotten'):
        service.begin_session(1, 'old', turn='late-reply')
    with pytest.raises(ValueError, match='forgotten'):
        service.get_context(scope, owner_id=1, session_id='old')


@pytest.mark.asyncio
async def test_forget_during_synthesis_cannot_publish_after_fresh_session(service):
    revoked = False
    async def synthesize(text, speed):
        nonlocal revoked
        if not revoked:
            revoked = True
            service.forget(1)
            service.begin_session(1, 'fresh', turn='actual-new-input')
        return b'\x01\x00' * 240
    service.synthesize = synthesize
    with pytest.raises(ValueError, match='forgotten'):
        await service.prepare_task(dict(scope_type='lesson', start=2, proposals={'2': {'words': ['尽头','依靠']}}), owner_id=1, session_id='inflight', paired_device_id='tv', callback_registration_id='cb')
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0] == 0


def test_copy_migration_twice_preserves_legacy_completion_and_chat(tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('migration_probe',Path(__file__).parents[1]/'scripts/verify_teaching_migrations.py')
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    source=tmp_path/'legacy.sqlite'
    with sqlite3.connect(source) as db:
        from test_dictation_repository import task,ev
        from lira.dictation_repository import DictationRepository
        repository=DictationRepository(db);legacy=task()
        repository.submit(legacy,paired_device_id='tv',owner_id=1)
        repository.receive(ev(legacy,1,'started'),paired_device_id='tv')
        repository.receive(ev(legacy,2,'lesson_dictation_completed',legacy['lessons'][0]['lesson_id']),paired_device_id='tv')
        db.execute('CREATE TABLE old_completion(task_id TEXT,status TEXT)')
        db.execute("INSERT INTO old_completion VALUES('old','completed')")
        db.execute('CREATE TABLE chat_history(text TEXT)')
        db.execute("INSERT INTO chat_history VALUES('ordinary chat remains')")
    before=source.read_bytes()
    report=probe.verify_copy(source,tmp_path/'rehearsal')
    assert report['passed'] and report['failure_rollback']
    assert source.read_bytes()==before
    with sqlite3.connect(report['migration_test_copy']) as db:
        assert db.execute('SELECT * FROM old_completion').fetchall()==[('old','completed')]
        assert db.execute('SELECT count(*) FROM dictation_result_revisions').fetchone()[0]==0
        assert DictationRepository(db).snapshot(legacy['task_id'])==legacy


def test_voice_actual_input_rotates_revoked_session(service):
    from lira.dictation_tools import DictationTools
    tools=DictationTools(service,lambda:1,lambda:'conversation')
    tools.event('user_text','默写第二课')
    old=tools.session_id()
    service.forget(1)
    # A model tool before a new actual user input retains the revoked identity.
    with pytest.raises(ValueError,match='forgotten'):service._session(1,old)
    tools.event('user_text','重新默写第二课')
    assert tools.session_id()!=old
    assert service._session(1,tools.session_id())['owner_id']==1


def test_failed_cloud_receipt_cannot_be_relabelled_passed(tmp_path):
    import importlib.util,json
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('evaluation_probe',Path(__file__).parents[1]/'scripts/evaluate_teaching_agent.py')
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    path=tmp_path/'failed.json'
    path.write_text(json.dumps(dict(status='failed',reason='clarification guard',counters=dict(regression_runs_started=1,initial_requests=1,new_task_ids=0,task_completed=0,prepare_after_ready=0))),encoding='utf8')
    result=probe.inspect_dictation_report(path)
    assert not result['mechanical_pass'] and not result['model_called']
    path.write_text(json.dumps(dict(status='completed',evidence_type='OFFLINE synthetic; not cloud evidence',counters=dict(regression_runs_started=1,initial_requests=1,new_task_ids=1,task_completed=1,prepare_after_ready=0),cleanup=dict(browser_closed=True,worker_done=True,server_pending=0,pending_continue_inputs=0,h5=dict(audioActive=False,timers=0,pendingInputs=0)))),encoding='utf8')
    result=probe.inspect_dictation_report(path)
    assert result['mechanical_pass'] and result['evidence_type'].startswith('OFFLINE')
