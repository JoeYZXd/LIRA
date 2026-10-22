import asyncio
import pytest
from lira.agent_task_state import AgentTaskState
from lira.agent_timers import AgentTimers
from lira.realtime_capabilities import CapabilityRegistry, CapabilityExecutor

async def setup(**kwargs):
    async def yes(*args):return True
    state=AgentTaskState(yes); state.begin('g','child')
    state.observe_user('请设置计时')
    await state.call('propose_agent_task',{'revision':0,'terms':'可以使用计时'})
    await state.call('activate_agent_task',{'revision':1,'turn_id':state.turn['id']})
    timers=AgentTimers(state, **kwargs)
    return state,timers

@pytest.mark.asyncio
async def test_timer_queues_once_and_wait_returns_real_event():
    state,t=await setup()
    created=await t.call('create_task_timer',{'revision':2,'duration_seconds':.02})
    result=await t.call('wait_for_task_event',{'revision':2,'timeout_seconds':1})
    assert result['event']['timer_id']==created['timer_id']
    assert result['event']['type']=='timer_expired'
    assert (await t.call('poll_task_events',{'revision':2}))['events']==[]
    assert (await t.call('wait_for_task_event',{'revision':2,'timeout_seconds':.01}))['timed_out']
    t.close()

@pytest.mark.asyncio
async def test_update_cancel_revisions_and_no_stale_callback():
    _,t=await setup()
    c=await t.call('create_task_timer',{'revision':2,'duration_seconds':.02})
    a={'revision':2,'timer_id':c['timer_id'],'timer_revision':1,'duration_seconds':.07}
    assert (await t.call('update_task_timer',a))['timer_revision']==2
    assert (await t.call('update_task_timer',a))['status']=='conflict'
    await asyncio.sleep(.03)
    assert not (await t.call('poll_task_events',{'revision':2}))['events']
    assert (await t.call('cancel_task_timer',dict(a,timer_revision=2)))['status']=='ok'
    await asyncio.sleep(.07)
    assert not (await t.call('poll_task_events',{'revision':2}))['events']
    t.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['update_task_timer', 'cancel_task_timer'])
async def test_terminal_timer_rejects_late_mutation_and_preserves_expiry(action):
    _,t=await setup()
    created=await t.call('create_task_timer',{'revision':2,'duration_seconds':.001})
    # Await the actual expiry callback without consuming its queued evidence.
    await t.timers[created['timer_id']]['task']
    original=t._snapshot(t.timers[created['timer_id']])
    event=dict(t.events[0])
    args={'revision':2,'timer_id':created['timer_id'],'timer_revision':1}
    if action=='update_task_timer':args['duration_seconds']=10
    try:
        assert await t.call(action,args)=={
            'status':'conflict','timer_revision':1,'timer_status':'expired'}
        assert t._snapshot(t.timers[created['timer_id']])==original
        assert (await t.call('poll_task_events',{'revision':2}))['events']==[event]
        assert (await t.call('poll_task_events',{'revision':2}))['events']==[]
    finally:
        t.close()

@pytest.mark.asyncio
async def test_cancelled_timer_rejects_new_calls_but_replays_same_call_id():
    _,t=await setup()
    registry=CapabilityRegistry();t.register(registry);ex=CapabilityExecutor(registry)
    try:
        created=await ex.call('create_task_timer',{'revision':2,'duration_seconds':10},'create')
        args={'revision':2,'timer_id':created['timer_id'],'timer_revision':1}
        cancelled=await ex.call('cancel_task_timer',args,'cancel')
        assert cancelled['timer']['status']=='cancelled'
        assert await ex.call('cancel_task_timer',args,'cancel')==cancelled
        current=dict(args,timer_revision=2)
        assert await ex.call('cancel_task_timer',current,'late-cancel')=={
            'status':'conflict','timer_revision':2,'timer_status':'cancelled'}
        assert await ex.call('update_task_timer',dict(current,duration_seconds=1),'late-update')=={
            'status':'conflict','timer_revision':2,'timer_status':'cancelled'}
        assert t.timers[created['timer_id']]['timer_revision']==2
        assert not t.events
    finally:
        t.close();await ex.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['pause_agent_task','stop_agent_task','revise_agent_task','invalidate','owner'])
async def test_permission_change_immediately_revokes_sleep_wait_and_queued_events(action):
    state,t=await setup()
    await t.call('create_task_timer',{'revision':2,'duration_seconds':10})
    waiting=asyncio.create_task(t.call('wait_for_task_event',{'revision':2,'timeout_seconds':30}))
    await asyncio.sleep(0)
    if action=='invalidate':state.invalidate()
    elif action=='owner':state.begin('other','other')
    else:await state.call(action,{'revision':2,'terms':'新条款'})
    assert (await asyncio.wait_for(waiting,.1))['status']=='cancelled'
    assert not t.timers and not t.events
    assert (await t.call('create_task_timer',{'revision':2,'duration_seconds':1}))['status']=='conflict'
    t.close()

@pytest.mark.asyncio
async def test_bounded_queue_reserves_capacity_and_registry_idempotency():
    state,t=await setup(max_events=1)
    registry=CapabilityRegistry();t.register(registry); ex=CapabilityExecutor(registry)
    args={'revision':2,'duration_seconds':.01}
    first=await ex.call('create_task_timer',args,'a')
    assert await ex.call('create_task_timer',args,'a')==first
    assert (await t.call('create_task_timer',args))['status']=='busy'
    await asyncio.sleep(.03)
    assert (await t.call('create_task_timer',args))['status']=='busy'
    events=await ex.call('poll_task_events',{'revision':2},'b')
    assert len(events['events'])==1
    assert await ex.call('poll_task_events',{'revision':2},'b')==events
    assert (await t.call('poll_task_events',{'revision':2}))['events']==[]
    assert (await t.call('cancel_task_timer',{'revision':2,'timer_id':'missing','timer_revision':1}))['status']=='unknown'
    with pytest.raises(ValueError):await ex.call('create_task_timer',dict(args,duration_seconds=-1),'invalid')
    t.close();await ex.close()

@pytest.mark.asyncio
async def test_concurrent_waiters_do_not_duplicate_or_report_false_expiry():
    state,t=await setup()
    await t.call('create_task_timer',{'revision':2,'duration_seconds':.01})
    answers=await asyncio.gather(*[t.call('wait_for_task_event',{'revision':2,'timeout_seconds':.04}) for _ in range(2)])
    assert sum(a['event'] is not None for a in answers)==1
    assert sum(a['timed_out'] for a in answers)==1
    await t.call('create_task_timer',{'revision':2,'duration_seconds':.01})
    await asyncio.sleep(.03)
    assert len(t.events)==1
    state.invalidate() # Same path used by privacy/disconnect/session stop.
    assert not t.events
    t.close()

@pytest.mark.asyncio
async def test_executor_wait_can_be_cancelled_without_blocking_other_calls():
    state,t=await setup()
    registry=CapabilityRegistry();t.register(registry);ex=CapabilityExecutor(registry)
    wait=asyncio.create_task(ex.call('wait_for_task_event',{'revision':2,'timeout_seconds':30},'wait'))
    await asyncio.sleep(.01)
    assert (await ex.call('list_task_timers',{'revision':2},'list'))['status']=='ok'
    operation=next(o for o in ex.metadata() if o['name']=='wait_for_task_event')
    await ex.call('cancel_capability_operation',{'operation_id':operation['operation_id']},'cancel')
    assert (await wait)['status']=='cancelled'
    state.invalidate();t.close();await ex.close()
