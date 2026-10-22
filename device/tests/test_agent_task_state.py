import pytest
from lira.agent_task_state import AgentTaskState

@pytest.mark.asyncio
async def test_unobserved_and_unverified_confirmation_cannot_activate():
    s = AgentTaskState()
    s.begin('session', 'owner')
    p = await s.call('propose_agent_task', {'revision':0,'terms':'孩子自行说下一项'})
    result = await s.call('activate_agent_task', {'revision':p['revision'],'turn_id':'invented'})
    assert result['status'] == 'needs_input'
    assert s.snapshot()['agreement'] is None

@pytest.mark.asyncio
async def test_revision_and_observed_turn_ownership_and_stop():
    async def verify(terms, text, delivered):
        return text == '等我说写完再继续'
    s=AgentTaskState(verify)
    s.begin('g1','child1')
    s.observe_user('等我说写完再继续')
    p=await s.call('propose_agent_task',{'revision':0,'terms':'等孩子说写完才继续'})
    active=await s.call('activate_agent_task',{'revision':1,'turn_id':s.turn['id']})
    assert active['status']=='active'
    assert s.permits(active['revision'], 'g1', 'child1')
    assert active['agreement']['source']=='observed_user_turn'
    assert (await s.call('pause_agent_task',{'revision':1}))['status']=='conflict'
    paused=await s.call('pause_agent_task',{'revision':2})
    assert paused['status']=='paused' and not s.permits(2,'g1','child1')
    stopped=await s.call('stop_agent_task',{'revision':3})
    assert stopped['agreement'] is None
    s.begin('g2','child2')
    assert s.snapshot()['agreement'] is None and s.turn is None

@pytest.mark.asyncio
async def test_pending_proposal_no_confirmation_and_assistant_is_not_user():
    s=AgentTaskState(lambda *args: None)
    s.begin('g','o')
    await s.call('propose_agent_task',{'revision':0,'terms':'自行推进'})
    s.observe_assistant('好的自行推进')
    assert s.snapshot()['status']=='proposed'
    assert (await s.call('activate_agent_task',{'revision':1,'turn_id':'assistant'}))['status']=='needs_input'

@pytest.mark.asyncio
async def test_late_verifier_cannot_activate_new_turn_or_closed_session():
    import asyncio
    entered, release=asyncio.Event(),asyncio.Event()
    async def verify(*args):
        entered.set()
        await release.wait()
        return True
    s=AgentTaskState(verify); s.begin('g','o'); s.observe_user('按照这个来')
    await s.call('propose_agent_task',{'revision':0,'terms':'自由节奏'})
    job=asyncio.create_task(s.call('activate_agent_task',{'revision':1,'turn_id':s.turn['id']}))
    await entered.wait()
    s.invalidate(); release.set()
    assert (await job)['status']=='conflict'
    assert not s.permits(1,'g','o')

@pytest.mark.asyncio
async def test_verifier_uses_only_independent_json_result_and_fails_closed():
    from types import SimpleNamespace as NS
    from lira.agent_task_state import AgentAgreementVerifier
    class API:
        def __init__(self):self.body='{"accepted":true}'
        async def create(self, **kwargs):
            assert kwargs['messages'][1]['role']=='user'
            assert 'temperature' not in kwargs  # Provider defaults support kimi-k3 and other configured models.
            return NS(choices=[NS(message=NS(content=self.body))])
    api=API()
    rt=NS(privacy=NS(is_on=False),_cfg=NS(llm=NS(model='test')),
          _llm=NS(_client=NS(chat=NS(completions=api)), is_available=lambda:True))
    v=AgentAgreementVerifier(rt)
    assert await v('terms','voice','spoken terms')
    api.body='{"accepted":true,"evidence":"model self assertion"}'
    assert not await v('terms','voice',None)
    for body in ('{"accepted":1}', '{"accepted":"true"}', '[]', 'not json'):
        api.body=body
        assert not await v('terms','voice',None)
    rt.privacy.is_on=True
    assert not await v('terms','voice',None)

@pytest.mark.asyncio
async def test_delivery_requires_successful_current_playback_not_text_generation():
    import asyncio
    from lira.audio.doubao_realtime import RealtimeSession
    from types import SimpleNamespace as NS
    events=[]
    s=object.__new__(RealtimeSession)
    s.closed=False; s._discard_audio=False; s._input_revision=3
    s.on_event=lambda kind,text: events.append((kind,text))
    done=asyncio.get_running_loop().create_future()
    s.playback=done
    s._agreement_delivery={'task':done,'revision':3,'text':'按你说的安排','sent':False,
        'evidence':{'task':done,'queued_bytes':960,'consumed_bytes':960,'ended':True}}
    s._publish_agreement_delivery()
    assert not events
    done.set_result(None)
    s._input_revision=4
    s._publish_agreement_delivery()
    assert not events
    s._input_revision=3
    s._publish_agreement_delivery()
    assert events==[('assistant_delivered','按你说的安排')]
    s._publish_agreement_delivery()
    assert len(events)==1

@pytest.mark.asyncio
async def test_expired_user_source_and_transport_phase_separation():
    from lira.realtime_capabilities import CapabilityRegistry, CapabilityExecutor
    now=[0]
    async def verify(*args):return True
    s=AgentTaskState(verify,clock=lambda:now[0])
    s.begin('g','o'); s.observe_user('自己说写完再继续')
    await s.call('propose_agent_task',{'revision':0,'terms':'自己说写完再继续'})
    now[0]=121
    assert (await s.call('activate_agent_task',{'revision':1,'turn_id':s.turn['id']}))['status']=='needs_input'
    registry=CapabilityRegistry(); s.register(registry)
    executor=CapabilityExecutor(registry)
    result=await executor.call('read_agent_task',{},'call1')
    assert result['status']=='ok' and result['task']['status']=='proposed'
    assert executor.metadata()[0]['status']=='completed'
    await executor.close()
