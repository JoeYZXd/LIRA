"""Offline guards: these tests never contact a cloud model."""
import importlib.util
import json
from pathlib import Path
import pytest

RUNNER=Path(__file__).parents[1]/'scripts'/'run_dictation_h5_regression.py'

def module():
    spec=importlib.util.spec_from_file_location('dictation_regression',RUNNER)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_fixture_and_hard_limits():
    m=module();f=m.load_fixture()
    assert len(f['two'])==18 and len(f['four'])==12
    assert all(any(c in w for w in f['words']) for c in f['four'])
    assert f['max_seconds']==900 and f['max_child_turns']==60

def test_context_may_follow_textbook_clarification_but_must_precede_prepare():
    m=module()
    first=dict(status='response',text='请问使用哪本教材？',trace=[])
    prepared=dict(trace=[dict(tool='get_dictation_context'),dict(tool='prepare_dictation_task')])
    m.check_preparation_trace(first,prepared)
    with pytest.raises(AssertionError,match='before preparation'):
        m.check_preparation_trace(first,dict(trace=[dict(tool='prepare_dictation_task')]))
    with pytest.raises(AssertionError,match='before preparation'):
        m.check_preparation_trace(first,dict(trace=[dict(tool='prepare_dictation_task'),dict(tool='get_dictation_context')]))

def test_durable_one_shot_lock(tmp_path):
    m=module();m.claim_run(tmp_path,'one')
    with pytest.raises(FileExistsError):m.claim_run(tmp_path,'one')
    with pytest.raises(ValueError):m.claim_run(tmp_path,'../escape')

def test_turn_budget_and_terminal_cleanup(tmp_path):
    m=module();r=m.RunRecord(tmp_path,'test',max_turns=1)
    r.child_turn('question')
    with pytest.raises(RuntimeError):r.child_turn('second')
    r.finish('failed',reason='budget')
    with pytest.raises(RuntimeError):r.child_turn('after terminal')
    assert json.loads((tmp_path/'report.json').read_text())['status']=='failed'

@pytest.mark.asyncio
async def test_failure_is_terminal_and_same_run_never_restarts(tmp_path,monkeypatch):
    m=module();calls=[]
    async def fail(*args):
        calls.append('once');raise RuntimeError('injected preflight failure')
    monkeypatch.setattr(m,'_execute',fail)
    result=await m.run_once(client=None,model='offline',synthesize=None,run_id='failure',artifact_dir=tmp_path)
    assert result['status']=='failed' and calls==['once']
    with pytest.raises(FileExistsError):await m.run_once(client=None,model='offline',synthesize=None,run_id='failure',artifact_dir=tmp_path)
    assert calls==['once']

@pytest.mark.asyncio
async def test_timeout_cancels_owned_execution_and_saves_terminal_report(tmp_path,monkeypatch):
    import asyncio
    m=module();f=m.load_fixture();f['max_seconds']=.01
    monkeypatch.setattr(m,'load_fixture',lambda:f)
    cleanup=[]
    async def waiting(*args):
        try:await asyncio.sleep(100)
        finally:cleanup.append('cancelled and closed')
    monkeypatch.setattr(m,'_execute',waiting)
    result=await m.run_once(client=None,model='offline',synthesize=None,run_id='timeout',artifact_dir=tmp_path)
    assert result['status']=='timeout' and cleanup==['cancelled and closed']
    assert json.loads((tmp_path/'timeout'/'report.json').read_text())['status']=='timeout'

@pytest.mark.asyncio
async def test_h5_protocol_auth_and_event_ack_loss_deduplication():
    module()  # Adds isolated test-terminal import path only.
    import httpx
    from h5_tv.server import TVServer
    server=TVServer();server.online=True
    server.task=dict(task_id='one',content_hash='hash');server.epoch=1
    event=dict(task_id='one',content_hash='hash',run_epoch=1,sequence=1,event_id='stable',state='paused')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as c:
        assert (await c.get('/status')).status_code==401
        assert (await c.get('/status',headers={'Authorization':'Bearer '+server.token})).status_code==200
        assert (await c.post('/sim/event',json=event)).status_code==200
        assert (await c.post('/sim/event',json=event)).json()['duplicate']
        assert len(server.events)==1
        assert (await c.post('/sim/event',json=dict(event,event_id='conflict'))).status_code==409
        assert (await c.post('/dictation/events',json=dict(task_id='one',content_hash='hash',run_epoch=2),headers={'Authorization':'Bearer '+server.token})).status_code==409

@pytest.mark.asyncio
async def test_h5_asset_scope_frames_and_atomic_digest_before_start():
    module()
    import httpx,hashlib,base64
    from h5_tv.server import TVServer
    s=TVServer();s.online=True;raw=b'\x01\x00'*10
    descriptor=dict(task_id='a',content_hash='a'*64,asset_id='voice',sha256=hashlib.sha256(raw).hexdigest(),pcm_bytes=20,frames=10,sample_rate=24000,channels=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=s.app),base_url='http://test',headers={'Authorization':'Bearer '+s.token}) as c:
        assert (await c.post('/dictation/assets/start',json=dict(descriptor,frames=11))).status_code==400
        assert (await c.post('/dictation/assets/start',json=descriptor)).status_code==200
        chunk=dict(task_id='a',content_hash='a'*64,asset_id='voice',offset=0,pcm_base64=base64.b64encode(raw).decode())
        assert (await c.post('/dictation/assets/chunk',json=dict(chunk,task_id='b'))).status_code==409
        assert (await c.post('/dictation/assets/chunk',json=chunk)).status_code==200
        assert (await c.post('/dictation/assets/commit',json=descriptor)).status_code==200
        assert (await c.post('/dictation/start',json=dict(task={'schema_version':2},manifest={},run_epoch=1))).status_code==400
        assert s.task is None

@pytest.mark.asyncio
async def test_h5_control_rejection_is_http_conflict():
    module()
    import httpx
    from h5_tv.server import TVServer
    s=TVServer();s.online=True;s.task=dict(task_id='a',content_hash='hash');s.epoch=1
    async def reject(action,payload):return dict(ok=False,reason='stale')
    s.dispatch=reject
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=s.app),base_url='http://test',headers={'Authorization':'Bearer '+s.token}) as c:
        assert (await c.post('/dictation/control',json=dict(task_id='a',content_hash='hash',run_epoch=1))).status_code==409
