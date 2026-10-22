import asyncio
import json
import pytest
from lira.realtime_capabilities import CapabilityRegistry, CapabilityExecutor

EMPTY = {'type':'object', 'properties':{}, 'additionalProperties':False}

@pytest.mark.asyncio
async def test_retention_is_not_a_lifetime_execution_limit():
    registry=CapabilityRegistry()
    seen=[]
    async def read(args):
        seen.append(1)
        return {'status':'ok','value':len(seen)}
    registry.register('read_value','test',EMPTY,read)
    executor=CapabilityExecutor(registry,max_operations=2,max_calls=3)
    for i in range(80):
        assert (await executor.call('read_value',{},str(i)))['status']=='ok'
    assert len(executor.calls)<=3 and len(executor.operations)<=2
    assert (await executor.call('read_value',{},'0'))['status']=='expired'
    assert len(seen)==80
    await executor.close()

@pytest.mark.asyncio
async def test_revocation_has_capacity_when_ordinary_operations_are_full():
    registry=CapabilityRegistry()
    async def slow(args):await asyncio.Event().wait()
    async def pause(args):return {'status':'ok','paused':True}
    registry.register('slow','test',EMPTY,slow,background=True)
    registry.register('pause_agent_task','test',EMPTY,pause)
    executor=CapabilityExecutor(registry,max_operations=1,max_calls=1)
    op=await executor.call('slow',{},'slow')
    assert (await executor.call('pause_agent_task',{},'pause'))['paused']
    assert (await executor.call('cancel_capability_operation',{'operation_id':op['operation_id']},'cancel'))['status']=='cancelled'
    await executor.close()

@pytest.mark.asyncio
async def test_session_more_than_64_calls_returns_expired_instead_of_silence():
    from lira.audio.doubao_realtime import RealtimeConfig,RealtimeSession
    class Socket:
        def __init__(self):self.sent=[]
        async def send(self,text):self.sent.append(json.loads(text))
        async def close(self):pass
    class Player:
        async def close(self):pass
        async def stop(self):pass
    registry=CapabilityRegistry()
    seen=[]
    async def read(args):seen.append(1);return {'status':'ok'}
    registry.register('read_value','test',EMPTY,read)
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *a:None,on_tool=None,capabilities=registry)
    session.ws=Socket()
    for i in range(80):
        await session._tool({'items':[{'name':'read_value','call_id':str(i),'arguments':'{}'}]})
        assert json.loads(session.ws.sent[-1]['items'][0]['content'][0]['text'])['status']=='ok'
    await session._tool({'items':[{'name':'read_value','call_id':'0','arguments':'{}'}]})
    assert json.loads(session.ws.sent[-1]['items'][0]['content'][0]['text'])['status']=='expired'
    assert len(seen)==80 and len(session._call_bindings)<=64 and len(session.calls)<=64
    await session.close()

def test_registry_rejects_unknown_and_invalid_without_execution():
    registry = CapabilityRegistry()
    with pytest.raises(ValueError):
        registry.validate('missing', {})
    registry.register('test', 'test', {'type':'object','properties':{'count':{'type':'integer','minimum':1}},'required':['count']}, None)
    for args in ({}, {'count': True}, {'count': 0}, {'count':1,'extra':'secret'}):
        with pytest.raises(ValueError):
            registry.validate('test', args)

@pytest.mark.asyncio
async def test_background_idempotency_cancel_and_generation():
    registry = CapabilityRegistry()
    entered, release = asyncio.Event(), asyncio.Event()
    executions = []
    async def slow(args):
        executions.append(args)
        entered.set()
        await release.wait()
        return {'status':'ok','text':'not persisted'}
    registry.register('capture_page', 'test', EMPTY, slow, background=True)
    executor = CapabilityExecutor(registry)
    one, two = await asyncio.gather(executor.call('capture_page', {}, 'call'), executor.call('capture_page', {}, 'call'))
    await entered.wait()
    assert one == two and len(executions) == 1
    assert one['status'] == 'pending'
    cancelled = await executor.call('cancel_capability_operation', {'operation_id':one['operation_id']}, 'cancel')
    assert cancelled['status'] == 'cancelled'
    release.set()
    await asyncio.sleep(0)
    assert (await executor.call('get_capability_operation', {'operation_id':one['operation_id']}, 'query'))['status'] == 'cancelled'
    await executor.close()
    new = CapabilityExecutor(registry)
    assert new.generation != executor.generation
    assert (await new.call('get_capability_operation', {'operation_id':one['operation_id']}, 'new'))['status'] == 'unknown'
    await new.close()

@pytest.mark.asyncio
async def test_real_operation_result_and_unavailable():
    registry = CapabilityRegistry()
    async def handler(args): return {'status':'ok','value':4}
    registry.register('read_page','test',EMPTY,handler,background=True)
    registry.register('unconfigured','test',EMPTY,handler,availability=lambda:'unavailable')
    executor = CapabilityExecutor(registry)
    assert (await executor.call('unconfigured',{},'missing'))['status'] == 'unavailable'
    operation = await executor.call('read_page',{},'first')
    await asyncio.sleep(.01)
    result = await executor.call('get_capability_operation',{'operation_id':operation['operation_id']},'query')
    assert result['status'] == 'completed' and result['result']['value'] == 4
    await executor.close()

@pytest.mark.asyncio
async def test_receive_consumes_new_turn_while_original_call_is_pending():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Socket:
        def __init__(self): self.events=asyncio.Queue(); self.sent=[]
        async def recv(self): return json.dumps(await self.events.get())
        async def send(self, text): self.sent.append(json.loads(text))
        async def close(self): pass
    class Player:
        async def close(self): pass
        async def stop(self): pass
    registry = CapabilityRegistry()
    entered, release = asyncio.Event(), asyncio.Event()
    count=[]
    async def slow(args):
        count.append(1); entered.set(); await release.wait()
        return {'status':'ok','value':731,'text':'SYNTHETIC_OCR_PRIVATE'}
    registry.register('slow_tool','test',EMPTY,slow)
    events=[]
    diagnostics=[]
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *e:events.append(e),on_tool=None,capabilities=registry,on_diagnostic=lambda *e:diagnostics.append(e))
    session.ws=socket=Socket()
    recv=asyncio.create_task(session._receive())
    event={'type':'response.function_call_arguments.done','items':[{'name':'slow_tool','call_id':'original','arguments':'{}'}]}
    await socket.events.put(event)
    await entered.wait()
    await socket.events.put(event)
    await socket.events.put({'type':'conversation.item.input_audio_transcription.completed','text':'new turn'})
    await socket.events.put({'type':'response.done'})
    await asyncio.sleep(.02)
    assert ('user_text','new turn') in events and ('response_done','') in events
    assert all(e['type'] == 'session.update' for e in socket.sent)
    release.set()
    await asyncio.sleep(.02)
    assert len(count) == 1
    assert all(e['type'] == 'session.update' for e in socket.sent)
    assert 'SYNTHETIC_OCR_PRIVATE' not in json.dumps(socket.sent)
    op = (await session.capabilities.call('list_capability_operations', {}, 'list'))['operations'][0]
    result = await session.capabilities.call('get_capability_operation', {'operation_id':op['operation_id']}, 'query')
    assert result['result']['value'] == 731
    assert 'SYNTHETIC_OCR_PRIVATE' not in json.dumps(diagnostics)
    assert any(e[0]=='local' for e in diagnostics) and any(e[0]=='sent' for e in diagnostics)
    recv.cancel(); await asyncio.gather(recv,return_exceptions=True)
    await session.close()

@pytest.mark.asyncio
async def test_closed_generation_discards_handler_that_returns_after_cancel():
    registry = CapabilityRegistry()
    entered = asyncio.Event()
    async def late(args):
        entered.set()
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            return {'status':'ok','text':'late private OCR'}
    registry.register('capture_late','test',EMPTY,late,background=True)
    executor=CapabilityExecutor(registry)
    operation=await executor.call('capture_late',{},'call')
    await entered.wait()
    await executor.close()
    assert executor.operations == {} and executor.calls == {}
    assert await executor.call('capture_late',{},'call') == {'status':'cancelled'}

@pytest.mark.asyncio
async def test_timeout_conflicting_call_and_bounded_capacity():
    registry=CapabilityRegistry()
    async def slow(args): await asyncio.sleep(20)
    schema={'type':'object','properties':{'text':{'type':'string'}},'required':['text']}
    registry.register('capture_page','test',schema,slow)
    executor=CapabilityExecutor(registry,timeout=.01,max_calls=1)
    assert (await executor.call('capture_page',{'text':'one'},'call'))['reason']=='timeout'
    assert (await executor.call('capture_page',{'text':'two'},'call'))['reason']=='call_id_conflict'
    assert (await executor.call('capture_page',{'text':'one'},'other'))['reason']=='timeout'
    await executor.close()

@pytest.mark.asyncio
async def test_deferred_results_are_discoverable_by_current_turn():
    registry = CapabilityRegistry()
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(args):
        entered.set()
        await release.wait()
        return {'status':'ok', 'value':912}
    registry.register('slow', 'test', EMPTY, slow)
    executor = CapabilityExecutor(registry)
    task = asyncio.create_task(executor.call('slow', {}, 'old'))
    await entered.wait()
    listed = await executor.call('list_capability_operations', {}, 'list1')
    op = listed['operations'][0]
    assert op['status'] == 'pending' and op['name'] == 'slow'
    assert 'result' not in op
    release.set()
    await task
    listed = await executor.call('list_capability_operations', {}, 'list2')
    assert listed['operations'][0]['status'] == 'completed'
    result = await executor.call('get_capability_operation', {'operation_id':op['operation_id']}, 'new')
    assert result['result']['value'] == 912
    await executor.close()

@pytest.mark.asyncio
async def test_capability_state_updates_preserve_identity_without_result_content():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Socket:
        def __init__(self): self.sent=[]
        async def send(self, text): self.sent.append(json.loads(text))
        async def close(self): pass
    class Player:
        async def close(self): pass
        async def stop(self): pass
    registry = CapabilityRegistry()
    async def read(args): return {'status':'ok','text':'PRIVATE_RESULT'}
    registry.register('read_private', 'test', EMPTY, read, background=True)
    session = RealtimeSession(config=RealtimeConfig(), player=Player(), on_event=lambda *a:None, on_tool=None, capabilities=registry)
    session.ws = socket = Socket()
    await session.set_identity_context({'status':'recognized','name':'TEST_NAME','address':'TEST_ADDRESS'})
    op = await session.capabilities.call('read_private', {}, 'start')
    await asyncio.sleep(.01)
    instructions = socket.sent[-1]['session']['instructions']
    assert 'TEST_NAME' in instructions and op['operation_id'] in instructions
    assert 'completed' in instructions and 'PRIVATE_RESULT' not in instructions
    assert '不能为此重复启动工作' in instructions
    assert '不能仅凭参数相同拒绝新的请求' in instructions
    await session.close()

@pytest.mark.asyncio
async def test_owner_reset_cancels_operations_and_isolates_old_call_results():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Player:
        async def stop(self):pass
        async def close(self):pass
    class Socket:
        def __init__(self):self.sent=[]
        async def send(self,text):self.sent.append(json.loads(text))
    entered, cancelled=asyncio.Event(),asyncio.Event()
    async def slow(args):
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return {'status':'ok','private':'old owner'}
    registry=CapabilityRegistry()
    registry.register('old_task','test',EMPTY,slow,background=True)
    async def fresh(args):return {'status':'ok','value':'new owner'}
    registry.register('new_task','test',EMPTY,fresh)
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *a:None,
                            on_tool=lambda *a:None,capabilities=registry)
    session.ws=Socket()
    old=session.capabilities
    op=await old.call('old_task',{},'old_call')
    await entered.wait()
    session.calls['legacy']={'status':'ok'}
    session.calls['old_call']=op
    session._capability_call_revisions['old_call']=0
    await session.reset_capability_owner()
    assert cancelled.is_set() and old.closed
    assert session.calls=={'legacy':{'status':'ok'}}
    assert session.capabilities.generation!=old.generation
    assert session.capabilities.metadata()==[]
    result=await session.capabilities.call('get_capability_operation',{'operation_id':op['operation_id']},'read')
    assert result['status']=='unknown'
    assert (await session.capabilities.call('new_task',{},'new'))['value']=='new owner'
    assert not any('old owner' in json.dumps(item) for item in session.ws.sent)
    await session.capabilities.close()

@pytest.mark.asyncio
async def test_session_call_id_binds_name_and_canonical_arguments_without_overwriting_result():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession, TOOLS
    class Socket:
        def __init__(self):self.sent=[]
        async def send(self,text):self.sent.append(json.loads(text))
        async def close(self):pass
    class Player:
        async def stop(self):pass
        async def close(self):pass
    executions=[]
    async def handler(args):
        executions.append(args)
        return {'status':'ok','value':args['value']}
    registry=CapabilityRegistry()
    schema={'type':'object','properties':{'value':{'type':'integer'}},'required':['value'],'additionalProperties':False}
    registry.register('read_value','test',schema,handler)
    registry.register('read_other','test',schema,handler)
    legacy_executions=[]
    async def legacy(name,args):
        legacy_executions.append(name)
        return {'status':'ok','value':'legacy'}
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *a:None,on_tool=legacy,capabilities=registry)
    session.ws=socket=Socket()
    async def call(name,args,call_id='bound'):
        await session._tool({'items':[{'name':name,'call_id':call_id,'arguments':args}]})
        return json.loads(socket.sent[-1]['items'][0]['content'][0]['text'])
    assert (await call('read_value','{"value":1}'))['value']==1
    original=session.calls['bound'].copy()
    for name,args in [('read_value','{"value":2}'),('read_other','{"value":1}'),(TOOLS[0]['name'],'{}')]:
        assert await call(name,args)=={'status':'rejected','reason':'call_id_conflict'}
        assert session.calls['bound']==original
    assert (await call('read_value','{ "value" : 1 }'))['value']==1
    assert executions==[{'value':1}]
    assert (await call('get_device_status','{}','legacy'))['value']=='legacy'
    legacy_result=session.calls['legacy'].copy()
    assert await call('read_value','{"value":1}','legacy')=={'status':'rejected','reason':'call_id_conflict'}
    assert session.calls['legacy']==legacy_result and legacy_executions==['get_device_status']
    await session.reset_capability_owner()
    assert 'legacy' in session._call_bindings
    assert 'bound' not in session._call_bindings
    await session.close()
    assert session._call_bindings=={}


@pytest.mark.asyncio
async def test_expired_operation_and_retained_conflict_and_inflight_limit():
    registry=CapabilityRegistry()
    async def read(args):return {'status':'ok','value':7}
    registry.register('read','test',EMPTY,read,background=True)
    executor=CapabilityExecutor(registry,max_operations=1,max_calls=2)
    first=await executor.call('read',{},'first')
    await asyncio.sleep(.01)
    second=await executor.call('read',{},'second')
    await asyncio.sleep(.01)
    assert (await executor.call('get_capability_operation',{'operation_id':first['operation_id']},'oldop'))['status']=='expired'
    actual=await executor.call('get_capability_operation',{'operation_id':second['operation_id']},'newop')
    assert actual['result']['value']==7
    assert (await executor.call('read',{},'newop'))['reason']=='call_id_conflict'
    await executor.close()

@pytest.mark.asyncio
async def test_ordinary_saturation_keeps_all_revocation_handlers_available():
    registry=CapabilityRegistry()
    async def slow(args):await asyncio.Event().wait()
    async def revoke(args):return {'status':'ok'}
    registry.register('slow','test',EMPTY,slow,background=True)
    for name in ('pause_agent_task','stop_agent_task','cancel_task_timer','clear_tv_content'):
        registry.register(name,'test',EMPTY,revoke)
    executor=CapabilityExecutor(registry,max_operations=32)
    ops=[await executor.call('slow',{},str(i)) for i in range(32)]
    assert (await executor.call('slow',{},'overflow'))['status']=='busy'
    for name in ('pause_agent_task','stop_agent_task','cancel_task_timer','clear_tv_content'):
        assert (await executor.call(name,{},name))['status']=='ok'
    assert (await executor.call('cancel_capability_operation',{'operation_id':ops[0]['operation_id']},'cancel'))['status']=='cancelled'
    await executor.close()


@pytest.mark.asyncio
async def test_session_full_inflight_bindings_reserve_control_reply():
    from lira.audio.doubao_realtime import RealtimeConfig,RealtimeSession
    class Socket:
        def __init__(self):self.sent=[]
        async def send(self,text):self.sent.append(json.loads(text))
        async def close(self):pass
    class Player:
        async def close(self):pass
        async def stop(self):pass
    registry=CapabilityRegistry()
    async def slow(args):await asyncio.Event().wait()
    registry.register('slow','test',EMPTY,slow)
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *a:None,on_tool=None,capabilities=registry)
    session.ws=Socket()
    session.capabilities.max_operations=64
    tasks=[asyncio.create_task(session._tool({'items':[{'name':'slow','call_id':str(i),'arguments':'{}'}]})) for i in range(64)]
    for _ in range(100):
        if len(session.capabilities.operations)==64:break
        await asyncio.sleep(.001)
    assert len(session.capabilities.operations)==64
    operation_id=next(iter(session.capabilities.operations))
    await session._tool({'items':[{'name':'cancel_capability_operation','call_id':'cancel','arguments':json.dumps({'operation_id':operation_id})}]})
    reply=next(row for row in session.ws.sent if row.get('items',[{}])[0].get('call_id')=='cancel')
    assert json.loads(reply['items'][0]['content'][0]['text'])['status']=='cancelled'
    await session.capabilities.close()
    await asyncio.gather(*tasks,return_exceptions=True)
    await session.close()

@pytest.mark.asyncio
async def test_close_exits_audio_sender_when_inflight_send_consumes_cancellation():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Player:
        async def stop(self): pass
        async def close(self): pass
    entered = asyncio.Event()
    sends = []
    session = RealtimeSession(config=RealtimeConfig(), player=Player(),
                              on_event=lambda *args: None, on_tool=None)
    async def raced_send(*args, **kwargs):
        sends.append(args)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A completion/cancellation race can return normally from an await.
            return True
    session.send = raced_send
    session.feed([.1] * 320)
    sender = session._spawn(session._send_audio())
    await entered.wait()
    closer = asyncio.create_task(session.close())
    done, _ = await asyncio.wait({closer}, timeout=.2)
    try:
        assert closer in done, 'close hung after cancellation was consumed'
        assert sender.done() and not session.tasks
        assert len(sends) == 1 and session.input.empty()
    finally:
        sender.cancel()
        closer.cancel()
        await asyncio.gather(sender, closer, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_drops_received_event_after_cancellation_completion_race(monkeypatch):
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Player:
        async def stop(self): pass
        async def close(self): pass
    entered = asyncio.Event()
    events = []
    class Socket:
        async def recv(self):
            entered.set()
            await asyncio.Event().wait()
        async def close(self): pass
    original_wait_for = asyncio.wait_for
    raced = False
    async def raced_wait_for(awaitable, timeout):
        nonlocal raced
        if timeout != 60:
            return await original_wait_for(awaitable, timeout)
        try:
            return await awaitable
        except asyncio.CancelledError:
            if raced:
                raise
            raced = True
            return json.dumps({'type':'response.output_text.done','text':'late'})
    monkeypatch.setattr(asyncio, 'wait_for', raced_wait_for)
    session = RealtimeSession(config=RealtimeConfig(), player=Player(),
                              on_event=lambda *args: events.append(args), on_tool=None)
    session.ws = Socket()
    receiver = session._spawn(session._receive())
    await entered.wait()
    closer = asyncio.create_task(session.close())
    done, _ = await asyncio.wait({closer}, timeout=.2)
    try:
        assert closer in done, 'close hung after receive completed during cancellation'
        assert receiver.done() and not session.tasks
        assert events == []
    finally:
        receiver.cancel()
        closer.cancel()
        await asyncio.gather(receiver, closer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['response.output_text.done', 'response.output_audio.done'])
async def test_close_drops_callbacks_after_branch_await_consumes_cancellation(kind):
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    entered = asyncio.Event()
    events = []
    cleaned = []
    class Player:
        async def stop(self): pass
        async def close(self): cleaned.append('player')
    class Socket:
        async def recv(self): return json.dumps({'type': kind, 'text': 'late'})
        async def close(self): cleaned.append('socket')
    session = RealtimeSession(config=RealtimeConfig(), player=Player(),
                              on_event=lambda *args: events.append(args), on_tool=None)
    session.ws = Socket()
    async def cancelled_branch(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return False
    if kind == 'response.output_text.done':
        # Exercise interrupt's final callback as well as the caller's callback.
        session._speech_request = {'revoked': False, 'done': asyncio.get_running_loop().create_future()}
        session.send = cancelled_branch
    else:
        session.output = asyncio.Queue()
        session._enqueue_output = cancelled_branch
    receiver = session._spawn(session._receive())
    await entered.wait()
    closer = asyncio.create_task(session.close())
    done, _ = await asyncio.wait({closer}, timeout=.2)
    try:
        assert closer in done, 'close must still terminate after cancellation is consumed'
        assert receiver.done() and not session.tasks
        assert events == []
        assert cleaned == ['player', 'socket']
    finally:
        receiver.cancel()
        closer.cancel()
        await asyncio.gather(receiver, closer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['response.output_audio.started', 'response.output_audio.delta'])
async def test_close_during_output_start_does_not_restart_player(kind):
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    entered = asyncio.Event()
    starts = []
    late_events = []
    class Player:
        stops = 0
        async def stop(self):
            self.stops += 1
            if self.stops == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return
        def play_stream(self, chunks):
            starts.append(True)
            async def play():
                async for _ in chunks: pass
            return play()
        async def close(self): pass
    class Socket:
        async def recv(self): return json.dumps({'type': kind, 'delta': 'AAA='})
        async def close(self): pass
    session = RealtimeSession(config=RealtimeConfig(), player=Player(),
                              on_event=lambda *args: late_events.append(args) if session.closed else None,
                              on_tool=None)
    session.ws = Socket()
    session._speech_request = {'revoked': False, 'started': False, 'text': 'late',
                               'done': asyncio.get_running_loop().create_future()}
    receiver = session._spawn(session._receive())
    await entered.wait()
    closer = asyncio.create_task(session.close())
    done, _ = await asyncio.wait({closer}, timeout=.2)
    try:
        assert closer in done
        assert receiver.done() and not session.tasks
        assert starts == [] and session.output is None and session.playback is None
        assert late_events == []
    finally:
        receiver.cancel()
        closer.cancel()
        await asyncio.gather(receiver, closer, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_from_tool_task_does_not_await_itself():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Player:
        async def stop(self): pass
        async def close(self): pass
    session = RealtimeSession(config=RealtimeConfig(), player=Player(),
                              on_event=lambda *args: None, on_tool=None)
    async def tool():
        await asyncio.sleep(0)
        await session.close()
    task = asyncio.create_task(tool())
    session._tool_tasks.add(task)
    done, _ = await asyncio.wait({task}, timeout=.2)
    try:
        assert task in done and not task.cancelled()
        assert task.exception() is None
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_from_session_task_does_not_await_itself():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Player:
        async def stop(self): pass
        async def close(self): pass
    session = RealtimeSession(config=RealtimeConfig(), player=Player(),
                              on_event=lambda *args: None, on_tool=None)
    async def worker():
        await asyncio.sleep(0)
        await session.close()
    task = session._spawn(worker())
    await asyncio.wait_for(task, 1)
    assert task.done() and not task.cancelled() and session.closed
