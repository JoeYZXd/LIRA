import asyncio
import sqlite3
from types import SimpleNamespace
import pytest
from lira.chat.store import ChatStore
from lira.agent_task_state import AgentTaskState
from lira.teaching_memory import TeachingMemory


def service(verifier=None):
    people = ChatStore(':memory:')
    member = people.create_member('孩子')
    task = AgentTaskState(); task.begin('g','o'); task.observe_user('以后都请慢一点播报')
    identity = SimpleNamespace(member_id=member['id'], context=lambda:{'status':'recognized','identity_source':'voiceprint','continuity_state':'stable'})
    rt = SimpleNamespace(store=SimpleNamespace(_conn=people._conn), privacy=SimpleNamespace(is_on=False), personal_memory=SimpleNamespace(people=people))
    async def accepted(*args): return True
    memory = TeachingMemory(rt, task, lambda:identity, verifier=verifier or accepted)
    return memory, task, identity, rt

@pytest.mark.asyncio
async def test_preference_requires_observed_current_source_and_semantics():
    async def temporary(*args):return False
    memory,task,identity,rt=service(temporary)
    result=await memory.propose({'kind':'preference','text':'慢一点','turn_id':task.turn['id']})
    assert result['status']=='needs_input'
    assert memory.read()['preferences']==[]
    memory.verifier=lambda *a:None
    assert (await memory.propose({'kind':'preference','text':'慢一点','turn_id':'fake'}))['status']=='needs_input'

@pytest.mark.asyncio
async def test_confirmed_owner_idempotency_override_and_isolation():
    memory,task,identity,rt=service()
    args={'kind':'preference','text':'慢一点','turn_id':task.turn['id']}
    first=await memory.propose(args); second=await memory.propose(args)
    assert first['id']==second['id']
    task.agreement={'terms':'本次快一点'};task.phase='active'
    assert memory.read()['current_agreement']==task.agreement
    identity.context=lambda:{'status':'collecting','name':'孩子'}
    assert memory.read()['status']=='unavailable'
    assert (await memory.propose(args))['status']=='unavailable'

@pytest.mark.asyncio
async def test_identity_changes_during_verification_fail_closed():
    memory,task,identity,rt=service()
    async def changed(*args):identity.member_id=None; return True
    memory.verifier=changed
    assert (await memory.propose({'kind':'preference','text':'慢一点','turn_id':task.turn['id']}))['status']=='conflict'
    assert rt.store._conn.execute('select count(*) from teaching_memories').fetchone()[0]==0


def test_parent_paper_review_revision_idempotency_and_forget():
    memory,task,identity,rt=service()
    mid=identity.member_id
    row=memory.parent_review(member_id=mid,record_id=None,expected_revision=0,request_id='p1',result='需改正',note='家长检查纸面')
    assert memory.parent_review(member_id=mid,record_id=None,expected_revision=0,request_id='p1',result='需改正',note='家长检查纸面')==row
    with pytest.raises(ValueError):memory.parent_review(member_id=mid,record_id=row['id'],expected_revision=0,request_id='p2',result='已核对',note='')
    fixed=memory.parent_review(member_id=mid,record_id=row['id'],expected_revision=1,request_id='p2',result='已核对',note='人工修正')
    assert fixed['revision']==2
    memory.forget(mid)
    assert memory.read()['status']=='unavailable'
    assert rt.store._conn.execute('select count(*) from teaching_memories').fetchone()[0]==0
    assert rt.store._conn.execute('select count(*) from teaching_reviews').fetchone()[0]==0

@pytest.mark.asyncio
async def test_registered_tools_cannot_grade_paper_or_choose_another_child():
    from lira.realtime_capabilities import CapabilityRegistry, CapabilityExecutor
    memory,task,identity,rt=service()
    registry=CapabilityRegistry();memory.register(registry)
    assert 'parent_review' not in registry.entries
    assert all(entry.sensitive for entry in registry.entries.values())
    with pytest.raises(ValueError):
        registry.validate('propose_teaching_memory',{'kind':'preference','text':'慢一点','turn_id':task.turn['id'],'member_id':999})

@pytest.mark.asyncio
async def test_learning_note_is_suggestion_not_a_grade_or_mastery():
    memory,task,identity,rt=service()
    saved=await memory.propose({'kind':'learning_note','text':'我已经全部掌握','turn_id':task.turn['id']})
    assert saved['body']['state']=='proposed_unverified'
    assert saved['source']=='observed_user_turn'
    assert memory.read()['preferences']==[]

@pytest.mark.asyncio
async def test_source_expiry_privacy_and_forget_during_verification():
    memory,task,identity,rt=service()
    args={'kind':'preference','text':'慢一点','turn_id':task.turn['id']}
    task.turn['observed_at']-=121
    assert (await memory.propose(args))['status']=='needs_input'
    task.observe_user('以后慢一点');args['turn_id']=task.turn['id']
    async def forget(*a):memory.forget(identity.member_id);return True
    memory.verifier=forget
    assert (await memory.propose(args))['status']=='conflict'
    rt.privacy.is_on=True
    assert memory.read()['status']=='unavailable'

@pytest.mark.asyncio
async def test_independent_preference_verifier_strict_json_and_failure():
    from lira.teaching_memory import TeachingPreferenceVerifier
    memory,task,identity,rt=service()
    class Client:
        async def create(self,**kwargs):
            assert 'temperature' not in kwargs
            assert kwargs['max_tokens']==300
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.value),finish_reason=getattr(self,'finish_reason','stop'))])
    client=Client();rt._llm=SimpleNamespace(_client=SimpleNamespace(chat=SimpleNamespace(completions=client)),is_available=lambda:True)
    rt._cfg=SimpleNamespace(llm=SimpleNamespace(model='test'))
    verifier=TeachingPreferenceVerifier(rt)
    for value,expected in [('{"accepted":true}',True),('{"accepted":"true"}',False),('{"accepted":true,"extra":1}',False),('invalid',False)]:
        client.value=value
        assert await verifier('慢一点','以后慢一点') is expected
    client.value='{"accepted":true}';client.finish_reason='length'
    assert not await verifier('慢一点','以后慢一点')
    rt.privacy.is_on=True
    assert not await verifier('慢一点','以后慢一点')


def test_member_delete_cascades_teaching_tables_and_migrations_idempotent():
    memory,task,identity,rt=service()
    mid=identity.member_id
    memory.parent_review(member_id=mid,record_id=None,expected_revision=0,request_id='q',result='已核对',note='')
    TeachingMemory(rt,task,lambda:identity)
    rt.personal_memory.people.delete_member(mid)
    assert rt.store._conn.execute('select count(*) from teaching_memories').fetchone()[0]==0
    assert rt.store._conn.execute('select count(*) from teaching_reviews').fetchone()[0]==0

@pytest.mark.asyncio
async def test_executor_reports_completed_for_successful_memory_and_source_conflict():
    from lira.realtime_capabilities import CapabilityRegistry,CapabilityExecutor
    memory,task,identity,rt=service()
    registry=CapabilityRegistry();memory.register(registry)
    executor=CapabilityExecutor(registry)
    args={'kind':'preference','text':'慢一点','turn_id':task.turn['id']}
    response=await executor.call('propose_teaching_memory',args,'one')
    assert response['status']=='ok'
    operations=await executor.call('list_capability_operations',{},'two')
    assert operations['operations'][0]['status']=='completed'
    changed=await memory.propose({**args,'text':'快一点'})
    assert changed['status']=='conflict'
    await executor.close()

@pytest.mark.asyncio
async def test_runtime_forget_closes_cloud_purges_archive_and_allows_new_context():
    from test_conversation_paths import runtime,FakeSession
    rt,paths=runtime()
    db=rt.store._conn
    db.execute('CREATE TABLE members(id INTEGER PRIMARY KEY,name TEXT)')
    db.execute("INSERT INTO members VALUES (1,'孩子')");db.commit()
    rt.personal_memory=SimpleNamespace(people=SimpleNamespace(get_member=lambda mid:dict(db.execute('SELECT * FROM members WHERE id=?',(mid,)).fetchone()) if mid==1 else None))
    paths.speaker_input.member_id=1
    paths.speaker_input.context=lambda:{'status':'recognized','identity_source':'voiceprint','continuity_state':'stable'}
    paths.teaching_memory.verifier=lambda *args: asyncio.sleep(0,result=True)
    paths.agent_task.begin('first','child');paths.agent_task.observe_user('以后慢一点')
    paths.history.begin();paths.history.event('user_text','私密教学内容')
    args={'kind':'preference','text':'慢一点','turn_id':paths.agent_task.turn['id']}
    assert (await paths.teaching_memory.propose(args))['status']=='ok'
    session=FakeSession();paths.session=session
    rt.engine.state=SimpleNamespace(value="listening")
    cleanup=paths.forget_member_now(1)
    assert paths.teaching_memory.read()['status']=='unavailable'
    assert paths.agent_task.generation is None
    await cleanup
    assert session.closed
    assert rt.engine.state.value=="listening"
    assert db.execute('select count(*) from voice_history_turns').fetchone()[0]==0
    assert db.execute('select count(*) from teaching_memories').fetchone()[0]==0
    paths.agent_task.begin('new','child');paths.agent_task.observe_user('以后慢一点')
    paths.speaker_input.member_id=1
    assert (await paths.teaching_memory.propose({**args,'turn_id':paths.agent_task.turn['id']}))['status']=='ok'
    await paths.close()

@pytest.mark.asyncio
async def test_exact_delivered_evidence_requires_successful_drain_not_model_claim():
    from lira.realtime_speech import RealtimeSpeech,speech_call
    memory,task,identity,rt=service()
    release=asyncio.Event();entered=asyncio.Event()
    class Session:
        closed=False
        _input_revision=0
        async def wait_speech_slot(self,**kwargs):return True
        async def speak_text(self,*a,**kwargs):entered.set();await release.wait();return self.success
        success=True
    session=Session();speech=RealtimeSpeech(rt,lambda:session,task)
    token=speech_call.set({'revision':0})
    try:
        operation=asyncio.create_task(speech.speak({'text':'实播提示'}))
        await entered.wait()
        assert list(speech.delivered)==[]
        release.set();result=await operation
        assert result['status']=='completed'
        assert speech.delivered[0]['source']=='exact_speech_drained'
        assert speech.delivered[0]['delivered_at']>=task.turn['observed_at']
        session.success=False
        assert (await speech.speak({'text':'未成功的提示'}))['status']=='interrupted'
        assert len(speech.delivered)==1
    finally:speech_call.reset(token)


def test_paper_review_reused_request_cannot_change_target_version_or_payload():
    memory,task,identity,rt=service();mid=identity.member_id
    args=dict(member_id=mid,record_id=None,expected_revision=0,request_id='q',result='已核对',note='')
    row=memory.parent_review(**args)
    for changed in ({'record_id':row['id']},{'expected_revision':1},{'result':'未核对'}):
        with pytest.raises(ValueError):memory.parent_review(**(args|changed))


def test_ui_delete_awaits_runtime_cleanup_before_member_disappears(tmp_path):
    from fastapi.testclient import TestClient
    from lira.appliances.store import ApplianceStore
    from lira.chat.service import ChatService
    from lira.privacy import PrivacyState,PassphraseVault
    from lira.ui.app import UiServices,create_app
    store=ApplianceStore(':memory:');people=ChatStore(tmp_path/'ui.db');privacy=PrivacyState()
    member=people.create_member('孩子');steps=[]
    async def cleanup(mid):
        assert people.get_member(mid) is not None
        await asyncio.sleep(0)
        steps.append('closed')
    services=UiServices(privacy=privacy,vault=PassphraseVault(store),store=store,
        chat=ChatService(people,None,privacy=lambda:False,remote_available=lambda:False),member_deleted=cleanup)
    with TestClient(create_app(services)) as client:
        assert client.post(f"/chat/{member['id']}/delete",follow_redirects=False).status_code==303
    assert steps==['closed'] and people.get_member(member['id']) is None
    people.close();store.close()

@pytest.mark.asyncio
async def test_forgetting_inactive_member_preserves_other_members_unrelated_archive():
    from test_conversation_paths import runtime,FakeSession
    rt,paths=runtime();db=rt.store._conn
    db.execute('CREATE TABLE members(id INTEGER PRIMARY KEY,name TEXT)')
    db.executemany('INSERT INTO members VALUES (?,?)',[(1,'甲'),(2,'乙')]);db.commit()
    paths.speaker_input.member_id=2
    paths.history.begin();paths.history.event('user_text','乙的独立历史')
    sid=paths.history.session_id
    paths.session=FakeSession()
    await paths.forget_member(1)
    assert db.execute('SELECT user FROM voice_history_turns WHERE session_id=?',(sid,)).fetchone()[0]=='乙的独立历史'
    await paths.close()
