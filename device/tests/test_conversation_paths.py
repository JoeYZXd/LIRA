import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

from lira.appliances.store import ApplianceStore
from lira.privacy import PrivacyState


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.frames = []

    async def open(self):
        pass

    async def close(self):
        self.closed = True

    def feed(self, samples):
        self.frames.append(samples.copy())


def runtime():
    from lira.conversation_paths import ConversationPaths
    class Engine:
        dnd = False
        state = SimpleNamespace(value='standby')
        def close(self):
            self.state = SimpleNamespace(value='standby')
    class Tts:
        async def stop(self):
            pass
    rt = SimpleNamespace(store=ApplianceStore(':memory:'), privacy=PrivacyState(),
                         engine=Engine(), tts=Tts(), wakeword='小鹿',
                         dispatcher=SimpleNamespace(clear=lambda: None),
                         pipeline=SimpleNamespace(playback_stop=lambda: None),
                         clear_voice_identity=lambda enabled: None,
                         wake_stream=SimpleNamespace(feed_and_poll=lambda s: None),
                         _mic_paused=False, telemetry=None, voice_enrollment=None)
    paths = ConversationPaths(rt, session_factory=FakeSession,
                              env={'LIRA_DOUBAO_REALTIME_API_KEY':'secret',
                                   'LIRA_DOUBAO_REALTIME_VOICE':'voice'})
    return rt, paths


@pytest.mark.asyncio
async def test_switch_persist_wake_and_switch_back_closes_session():
    rt, paths = runtime()
    await paths.switch('doubao_realtime')
    assert rt.store.get_meta('conversation_path') == 'doubao_realtime'
    assert paths.feed(np.zeros(1600, np.float32)) is True
    assert paths.session is None  # standby never uploads audio
    await paths.start()
    session = paths.session
    paths.feed(np.zeros(1600, np.float32))
    assert len(session.frames) == 1
    await paths.switch('classic')
    assert session.closed
    assert paths.feed(np.zeros(1600, np.float32)) is False
    await paths.close()


@pytest.mark.asyncio
async def test_missing_config_and_privacy_do_not_change_path():
    rt, paths = runtime()
    paths.config = type(paths.config)()
    with pytest.raises(ValueError):
        await paths.switch('doubao_realtime')
    assert paths.mode == 'classic'
    await rt.privacy.set_enabled(True, source='test')
    with pytest.raises(PermissionError):
        await paths.start()
    assert paths.session is None
    await paths.close()


@pytest.mark.asyncio
async def test_privacy_and_microphone_pause_close_cloud_session():
    rt, paths = runtime()
    await paths.switch('doubao_realtime')
    await paths.start()
    session = paths.session
    await rt.privacy.set_enabled(True, source='test')
    await paths.apply_privacy(True)
    assert session.closed and paths.session is None
    assert 'secret' not in json.dumps(paths.snapshot())
    await paths.close()


@pytest.mark.asyncio
async def test_production_dispatch_has_only_one_audio_consumer(tmp_path):
    from test_main_loop import make_rig
    from lira.audio.doubao_realtime import RealtimeConfig
    rig = make_rig(tmp_path)
    rt = rig.runtime
    paths = rt.conversation_paths
    paths.config = RealtimeConfig(api_key='secret', voice='voice')
    paths._session_factory = FakeSession
    await paths.switch('doubao_realtime')
    await paths.start()
    class ForbiddenAsr:
        def feed(self, samples):
            raise AssertionError('classic ASR received cloud audio')
        def reset(self): pass
    rt.asr_stream = ForbiddenAsr()
    rt.dispatcher.feed(np.zeros(1600, np.float32))
    session = paths.session
    assert len(session.frames)==1
    await rt.privacy.set_enabled(True, source='test')
    assert session.closed
    await paths.close()
    rt.store.close()


@pytest.mark.asyncio
async def test_failed_connection_never_routes_audio_to_classic():
    rt, paths = runtime()
    class BrokenSession(FakeSession):
        async def open(self):
            raise OSError('secret credential must not leak')
    paths._session_factory = BrokenSession
    await paths.switch('doubao_realtime')
    with pytest.raises(RuntimeError):
        await paths.start()
    assert paths.mode=='doubao_realtime' and paths.session is None
    assert paths.state=='error'
    assert 'credential' not in paths.error
    assert paths.feed(np.zeros(1600,np.float32)) is True
    await paths.close()


@pytest.mark.asyncio
async def test_stale_tool_cannot_start_enrollment_after_switch():
    rt, paths = runtime()
    await paths.switch('doubao_realtime')
    generation = paths._generation
    await paths.switch('classic')
    await paths._finish_tool_action(True, generation)
    assert paths.mode=='classic' and not paths._handoff
    await paths.close()


def test_authenticated_endpoint_switch_and_invalid_configuration():
    from test_dev_console import make_client, login
    from lira.conversation_paths import ConversationPaths
    _, client, rt = make_client()
    rt._mic_paused = False
    rt.conversation_paths = ConversationPaths(rt, env={})
    assert client.get('/dev/api/conversation-path').status_code == 401
    login(client)
    r = client.get('/dev/api/conversation-path')
    assert r.status_code == 200 and r.json()['mode']=='classic'
    r = client.post('/dev/api/conversation-path',json={'mode':'doubao_realtime'})
    assert r.status_code == 400 and rt.conversation_paths.mode=='classic'
    assert client.post('/dev/api/conversation-path',json={'mode':'bad'}).status_code==400
    rt.privacy._on = True
    assert client.post('/dev/api/conversation-path',json={'mode':'classic'}).status_code==403


@pytest.mark.asyncio
async def test_audio_stop_failure_still_closes_socket_and_clears_input():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    closed = []
    class BrokenPlayer:
        async def stop(self): raise RuntimeError('audio stop failed')
        async def close(self): raise RuntimeError('audio close failed')
    async def close_socket(): closed.append(True)
    session = RealtimeSession(config=RealtimeConfig(), player=BrokenPlayer(),
        on_event=lambda *a:None, on_tool=None)
    session.ws = SimpleNamespace(close=close_socket)
    session.feed(np.zeros(1600,np.float32))
    with pytest.raises(RuntimeError):
        await session.close()
    assert closed and session.input.empty() and session._tail==b''


@pytest.mark.asyncio
async def test_forbidden_handshake_explains_resource_permission_without_leaking_key():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession, RealtimeError
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    async def connect(*args, **kwargs):
        raise InvalidStatus(Response(403, 'Forbidden', Headers(),
            body=b'{"error":{"code":"45000030","message":"secret requested resource not granted"}}'))
    async def cleanup(): pass
    session = RealtimeSession(config=RealtimeConfig(api_key='secret',voice='voice'),
        player=SimpleNamespace(stop=cleanup,close=cleanup), connect=connect,
        on_event=lambda *a:None,on_tool=None)
    with pytest.raises(RealtimeError) as error:
        await session.open()
    assert '403' in str(error.value) and '资源授权' in str(error.value)
    assert 'secret' not in str(error.value)
    assert session.closed


@pytest.mark.asyncio
async def test_cancelled_start_cleans_session_during_ready_cue():
    rt, paths = runtime()
    entered = asyncio.Event()
    async def wait():
        entered.set()
        await asyncio.Event().wait()
    rt.settings = SimpleNamespace(volume=1)
    rt.tts.chime = lambda **kwargs: SimpleNamespace(wait=wait)
    await paths.switch('doubao_realtime')
    task = asyncio.create_task(paths.start())
    await entered.wait()
    session = paths.session
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert session.closed and paths.session is None and paths.state=='standby'
    await paths.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('save',[False,True])
async def test_cloud_enrollment_stays_connected_and_uses_real_local_store(tmp_path, save):
    from test_main_loop import make_rig
    from test_voice_enrollment import setup, audio, V
    from lira.audio.doubao_realtime import RealtimeConfig
    rig = make_rig(tmp_path)
    rt = rig.runtime
    memory, legacy = setup(tmp_path, lambda:rt.privacy.is_on)
    legacy.cancel()
    rt.personal_memory, rt.voice_enrollment = memory, legacy
    rt.speaker_identity = SimpleNamespace(embedding=lambda s:V, match=lambda v:None, pending=None)
    paths = rt.conversation_paths
    paths.config = RealtimeConfig(api_key='secret',voice='voice')
    paths._session_factory = FakeSession
    await paths.switch('doubao_realtime')
    await paths.start()
    session = paths.session
    async def forbidden(*a, **kw):
        raise AssertionError('Cloud enrollment must never use local ASR/LLM enrollment')
    rt.handle_voice_input = forbidden
    legacy.handle = forbidden
    assert (await paths._tool('start_voice_enrollment',{}))['status'] == 'details'
    assert paths.session is session and not session.closed and not legacy.active
    assert (await paths._tool('set_voice_enrollment_details',{'name':'张三','address':'小张'}))['status']=='details_set'
    if save:
        for seed in (1,2,3):
            await paths._tool('start_voice_sample',{})
            assert paths.feed(audio(seed)) is True
            paths._event('user_text', '我今天去公园散步，看到很多花')
            result=await paths._tool('check_voice_sample',{})
        assert result['status']=='ready_to_confirm' and not memory.profiles()
        paths.feed(audio(4))
        paths._event('user_text','我确认按这个姓名和称呼保存')
        assert (await paths._tool('save_voice_enrollment',{}))['status']=='saved'
        assert len(memory.profiles())==1
        assert memory.preferred_address(memory.profiles()[0][0])=='小张'
        assert paths.speaker_input.context()['status']=='recognized'
        assert paths.speaker_input.context()['name']=='张三'
    else:
        await paths._tool('start_voice_sample',{})
        paths.feed(audio())
        assert (await paths._tool('cancel_voice_enrollment',{}))['status']=='cancelled'
        assert not memory.profiles()
    assert paths.enrollment.snapshot()['status']=='idle'
    assert paths.session is session and not session.closed
    assert not legacy.active and not paths._handoff and paths.state=='listening'
    await paths.close()
    await memory.close()
    rt.store.close()


@pytest.mark.asyncio
async def test_realtime_protocol_stream_and_tool_result():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Socket:
        def __init__(self):
            self.sent = []
            self.incoming = asyncio.Queue()
            self.incoming.put_nowait(json.dumps({'type':'session.created'}))
        async def send(self, value):
            self.sent.append(json.loads(value))
        async def recv(self):
            return await self.incoming.get()
        async def close(self):
            pass
    socket = Socket()
    async def connect(*args, **kwargs):
        assert kwargs['additional_headers']['X-Api-Key'] == 'secret'
        return socket
    class Player:
        async def open(self): pass
        async def stop(self): pass
        async def close(self): pass
        async def play_stream(self, chunks):
            async for chunk in chunks: pass
    calls = []
    events = []
    diagnostics=[]
    from lira.usage import UsageLedger
    usage_store=ApplianceStore(':memory:')
    usage=UsageLedger(usage_store)
    async def tool(name, args):
        calls.append((name,args))
        return {'status':'ok'}
    session = RealtimeSession(config=RealtimeConfig(api_key='secret', voice='voice'),
                              player=Player(), on_event=lambda *a:events.append(a), on_tool=tool,
                              connect=connect,usage=usage,usage_source='test',
                              on_diagnostic=lambda *e:diagnostics.append(e))
    await session.open()
    assert 'extension' not in socket.sent[0]['session']
    session.feed(np.zeros(1600, np.float32))
    event = json.dumps({'type':'response.function_call_arguments.done',
        'items':[{'call_id':'c1','name':'get_device_status','arguments':'{}'}]})
    socket.incoming.put_nowait(json.dumps({'type':'conversation.item.input_audio_transcription.completed','text':'你好'}))
    socket.incoming.put_nowait(event)
    socket.incoming.put_nowait(event)
    await asyncio.sleep(.15)
    assert calls == [('get_device_status',{})]
    assert sum(e['type']=='input_audio_buffer.append' for e in socket.sent) == 5
    counters=usage.snapshot()['days'][-1]['test']
    assert counters['sessions']==1
    assert counters['input_seconds']==.1 and counters['zero_seconds']==.1
    result = next(e for e in socket.sent if e['type']=='conversation.item.create')
    assert result['items'][0]['call_id'] == 'c1'
    assert result['items'][0]['type'] == 'message'
    socket.incoming.put_nowait(json.dumps({'type':'response.done'}))
    await session.drain_input()
    await asyncio.sleep(.01)
    assert ('response_done','') in events
    assert any(direction=='sent' and kind=='session.create' and turn==0 for direction,kind,_,turn in diagnostics)
    assert any(kind=='conversation.item.input_audio_transcription.completed' and turn==1 for _,kind,_,turn in diagnostics)
    assert any(direction=='received' and kind=='response.done' and turn==1 for direction,kind,_,turn in diagnostics)
    assert all(kind!='input_audio_buffer.append' for _,kind,_,_ in diagnostics)
    assert 'X-Api-Key' not in json.dumps(diagnostics)
    socket.incoming.put_nowait(json.dumps({'type':'response.function_call_arguments.done',
        'items':[{'call_id':'c2','name':'start_voice_enrollment','arguments':'{"unexpected":true}'}]}))
    await asyncio.sleep(.03)
    assert ('tool_done','start_voice_enrollment') not in events
    await session.close()
    assert UsageLedger(usage_store).snapshot()['days'][-1]['test']['input_seconds']==.1
    usage_store.close()


@pytest.mark.asyncio
async def test_proactive_speech_uses_official_commit_and_assistant_history_after_drain():
    from lira.audio.doubao_realtime import RealtimeSession,RealtimeConfig
    incoming=asyncio.Queue();sent=[];events=[]
    release=asyncio.Event()
    class Socket:
        async def recv(self):return await incoming.get()
        async def send(self,data):sent.append(json.loads(data))
        async def close(self):pass
    class Player:
        async def stop(self):pass
        async def close(self):pass
        async def play_stream(self,chunks):
            async for _ in chunks:pass
            await release.wait()
    s=RealtimeSession(config=RealtimeConfig(),player=Player(),on_tool=None,on_event=lambda *e:events.append(e))
    s.ws=Socket();receiver=asyncio.create_task(s._receive())
    speech=asyncio.create_task(s.speak_text('你是张三吗？'))
    try:
        await asyncio.sleep(.01)
        assert sent[0]['type']=='speech_text_buffer.commit'
        import base64
        for event in ({'type':'response.output_audio.started'},
                      {'type':'response.output_audio.delta','delta':base64.b64encode(b'\x01\x00'*480).decode()},
                      {'type':'response.output_audio.done'}):
            incoming.put_nowait(json.dumps(event))
        await asyncio.sleep(.05)
        assert not speech.done()
        assert events.count(('assistant_prompt','你是张三吗？'))==1
        release.set()
        assert await asyncio.wait_for(speech,1)
        assert sent[-1]['type']=='conversation.item.create'
        assert sent[-1]['items'][0]['role']=='assistant'
        assert not any(k=='user_text' for k,_ in events)
        assert all(e['type']!='response.create' for e in sent)
    finally:
        release.set();speech.cancel();receiver.cancel()
        await asyncio.gather(speech,receiver,return_exceptions=True)
        await s.close()


@pytest.mark.asyncio
async def test_websearch_config_is_sent_to_cloud_and_secrets_are_not_exposed():
    from lira.conversation_paths import ConversationPaths
    from lira.audio.doubao_realtime import RealtimeSession
    rt, old = runtime()
    paths = ConversationPaths(rt,env={'LIRA_DOUBAO_REALTIME_API_KEY':'voice-secret',
        'LIRA_DOUBAO_REALTIME_VOICE':'voice','enable_volc_websearch':'true',
        'volc_websearch_type':'web_custom_api','volc_websearch_api_key':'search-secret'})
    assert paths.config.ready and paths.snapshot()['websearch_enabled']
    assert 'search-secret' not in str(paths.snapshot())+repr(paths.config)
    sent=[]
    class Socket:
        first=True
        async def send(self, data): sent.append(json.loads(data))
        async def recv(self):
            if self.first:
                self.first=False
                return '{"type":"session.created"}'
            await asyncio.Event().wait()
        async def close(self):pass
    class Player:
        async def open(self):pass
        async def stop(self):pass
        async def close(self):pass
    async def connect(*args,**kwargs):return Socket()
    session=RealtimeSession(config=paths.config,player=Player(),connect=connect,
                            on_event=lambda *args:None,on_tool=None)
    await session.open()
    assert 'extension' not in sent[0]['session']
    assert sent[0]['extension']['dialog']['extra']=={
        'enable_volc_websearch':True,'volc_websearch_type':'web_custom_api',
        'volc_websearch_api_key':'search-secret'}
    await session.close()
    await paths.close()
    await old.close()


@pytest.mark.asyncio
async def test_cloud_playback_echo_is_not_uploaded_but_wake_can_interrupt():
    rt, paths = runtime()
    await paths.switch('doubao_realtime')
    await paths.start()
    session = paths.session
    session.output_occupied = True
    interrupted = asyncio.Event()
    async def interrupt():
        interrupted.set()
    session.interrupt = interrupt
    rt.wake_stream = SimpleNamespace(feed_and_poll=lambda samples: None)
    echo = np.full(1600, .25, np.float32)
    paths.feed(echo)
    assert not np.any(session.frames[-1]), 'speaker echo must be replaced with silence'
    rt.wake_stream.feed_and_poll = lambda samples: '小鹿'
    paths.feed(echo)
    await asyncio.sleep(0)
    assert interrupted.is_set(), 'local wake must still interrupt protected playback'
    session.output_occupied = False
    paths.feed(echo)
    np.testing.assert_array_equal(session.frames[-1], echo)
    await paths.close()


@pytest.mark.asyncio
async def test_cloud_echo_guard_waits_for_physical_playback_and_tail(monkeypatch):
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    clock = [100.0]
    monkeypatch.setattr('lira.audio.doubao_realtime.time.monotonic', lambda: clock[0])
    drained = asyncio.Event()
    class Player:
        async def stop(self): pass
        async def play_stream(self, chunks):
            async for chunk in chunks: pass
            await drained.wait()
    session = RealtimeSession(config=RealtimeConfig(), player=Player(),
                              on_event=lambda *args: None, on_tool=None)
    await session._start_output()
    await session.output.put(None)  # Cloud EOF precedes physical drain.
    await asyncio.sleep(0)
    assert session.output_occupied
    clock[0] += 10
    assert session.output_occupied
    drained.set()
    await session.playback
    await asyncio.sleep(0)
    assert session.output_occupied, 'AEC tail must not become the next user turn'
    clock[0] += .31
    assert not session.output_occupied
    await session._stop_output()


@pytest.mark.asyncio
async def test_local_wake_interrupt_cancels_cloud_response_and_silences_player():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    sent, events = [], []
    class Player:
        stopped = False
        async def stop(self): self.stopped = True
    class Socket:
        async def send(self, data): sent.append(json.loads(data))
    player = Player()
    session = RealtimeSession(config=RealtimeConfig(), player=player,
                              on_event=lambda *args: events.append(args), on_tool=None)
    session.ws = Socket()
    await session.interrupt()
    assert player.stopped and session._discard_audio
    assert sent == [{'type':'response.cancel'}]
    assert events[-1][0] == 'interrupted'


@pytest.mark.asyncio
async def test_cloud_audio_burst_waits_for_player_instead_of_aborting_session():
    import base64
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    incoming = asyncio.Queue()
    for event in [{'type':'session.created'}, {'type':'response.output_audio.started'},
                  {'type':'response.output_audio.delta','delta':base64.b64encode(b'\1\0'*72000).decode()},
                  {'type':'response.output_audio.done'}, {'type':'response.done'}]:
        incoming.put_nowait(json.dumps(event))
    class Socket:
        async def send(self, value): pass
        async def recv(self): return await incoming.get()
        async def close(self): pass
    async def connect(*args, **kwargs): return Socket()
    class Player:
        consumed = 0
        async def open(self): pass
        async def close(self): pass
        async def stop(self): pass
        async def play_stream(self, chunks):
            async for chunk in chunks:
                await asyncio.sleep(.001)
                self.consumed += len(chunk)
    done = asyncio.Event()
    player = Player()
    from lira.usage import UsageLedger
    usage_store=ApplianceStore(':memory:')
    usage=UsageLedger(usage_store)
    session = RealtimeSession(config=RealtimeConfig(api_key='secret',voice='voice'),
                              player=player,connect=connect,on_tool=None,
                              usage=usage,
                              on_event=lambda k,t: done.set() if k in ('error','response_done') else None)
    try:
        await session.open()
        await asyncio.wait_for(done.wait(),2)
        assert not session.failed, 'a valid 3-second cloud burst must apply backpressure'
        await asyncio.wait_for(session.playback,2)
        assert player.consumed == 144000
        assert usage.snapshot()['days'][-1]['normal']['output_seconds']==3
    finally:
        await session.close()
        usage_store.close()


@pytest.mark.asyncio
async def test_full_audio_buffer_does_not_prevent_local_interrupt():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    class Player:
        async def stop(self): pass
        async def play_stream(self, chunks): await asyncio.Event().wait()
    class Socket:
        async def send(self, data): pass
    session = RealtimeSession(config=RealtimeConfig(),player=Player(),
                              on_event=lambda *args:None,on_tool=None)
    session.ws = Socket()
    await session._start_output()
    queue = session.output
    for _ in range(queue.maxsize): queue.put_nowait(b'\0\0')
    pending = asyncio.create_task(session._enqueue_output(queue,b'\0\0'))
    await asyncio.sleep(.01)
    await asyncio.wait_for(session.interrupt(), .3)
    assert not await asyncio.wait_for(pending,.3)


@pytest.mark.asyncio
async def test_realtime_error_reports_safe_reason_without_raw_exception():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession, RealtimeError
    events=[]
    session=RealtimeSession(config=RealtimeConfig(),player=None,on_tool=None,
                            on_event=lambda *args: events.append(args))
    async def broken(): raise RealtimeError('Audio output stalled')
    task=asyncio.create_task(broken())
    await asyncio.gather(task,return_exceptions=True)
    session._task_done(task)
    assert '音频输出持续停滞' in events[-1][1]
    async def secret_error(): raise OSError('credential-do-not-export')
    task=asyncio.create_task(secret_error())
    await asyncio.gather(task,return_exceptions=True)
    session._task_done(task)
    assert 'credential' not in str(events)


def test_silent_upload_backlog_keeps_latest_audio_without_ending_session():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    events=[]
    s=RealtimeSession(config=RealtimeConfig(),player=None,on_tool=None,
                      on_event=lambda *args:events.append(args))
    s.feed(np.zeros(1600*10,np.float32))
    assert not s.failed, 'redundant silence must not terminate playback'
    assert s.input.qsize()==s.input.maxsize
    speech=np.full(320,.2,np.float32)
    s.feed(speech)
    queued=[s.input.get_nowait() for _ in range(s.input.qsize())]
    assert queued[-1] == (speech*32768).astype('<i2').tobytes()
    assert not any(k=='error' for k,t in events)
    assert sum(k=='input_silence_coalesced' for k,t in events)==1


def test_real_speech_upload_backlog_still_stops_instead_of_dropping_words():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    events=[]
    s=RealtimeSession(config=RealtimeConfig(),player=None,on_tool=None,
                      on_event=lambda *args:events.append(args))
    s.feed(np.full(1600*6,.2,np.float32))
    assert s.failed and any(k=='error' for k,t in events)
    assert not any(k=='input_silence_coalesced' for k,t in events)


@pytest.mark.asyncio
async def test_silence_coalescing_recovers_after_send_stall_and_drain_finishes():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    entered,release=asyncio.Event(),asyncio.Event()
    sent=[]
    class Socket:
        async def send(self,data):
            entered.set()
            await release.wait()
            sent.append(json.loads(data))
        async def close(self):pass
    class Player:
        async def stop(self):pass
        async def close(self):pass
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),
                            on_event=lambda *args:None,on_tool=None)
    session.ws=Socket()
    session._spawn(session._send_audio())
    try:
        session.feed(np.zeros(1600,np.float32))
        await asyncio.wait_for(entered.wait(),1)
        session.feed(np.zeros(16000,np.float32))
        assert not session.failed and session.input.qsize()==25
        release.set()
        await asyncio.wait_for(session.drain_input(),2)
        assert not session._input_silence
        assert len(sent)<55, 'old silence should not be replayed after recovery'
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('threshold,noise', [(0, 0), (.003, .001)])
async def test_long_zero_input_upload_is_bounded_and_speech_resumes_without_loss(monkeypatch, threshold, noise):
    import base64
    import lira.audio.doubao_realtime as module
    real_asyncio = asyncio
    clock = [1000.0]
    class Scheduler:
        def __getattr__(self, name): return getattr(real_asyncio, name)
        async def sleep(self, delay):
            clock[0] += max(0, delay)
            await real_asyncio.sleep(0)
    monkeypatch.setattr(module, 'asyncio', Scheduler())
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    counters = {}
    class Usage:
        def add(self, source, field, amount):
            counters[field] = counters.get(field, 0) + amount
    session = module.RealtimeSession(config=module.RealtimeConfig(silence_rms=threshold), player=None,
        on_tool=None, on_event=lambda *args: None, usage=Usage())
    session.input = real_asyncio.Queue(maxsize=1600)
    sent = []
    async def send(kind, **data):
        assert kind == 'input_audio_buffer.append'
        sent.append(base64.b64decode(data['audio']))
    session.send = send
    sender = real_asyncio.create_task(session._send_audio())
    try:
        # Thirty seconds of playback guard must not upload thirty seconds of zeros.
        session.feed(np.full(320 * 1500, noise, np.float32))
        await session.drain_input()
        assert len(sent) == 100, 'retain only 2 seconds for server endpointing'
        assert counters['zero_seconds'] == pytest.approx(2)
        assert not session._input_silence
        # Resume on the first eligible frame; disabling the gate retains even
        # minimum-amplitude nonzero PCM.
        speech = np.full(320, .01 if threshold else 1 / 32768, np.float32)
        session.feed(speech[:123])
        session.feed(speech[123:])
        await session.drain_input()
        assert sent[-1] == (speech * 32768).astype('<i2').tobytes()
        assert len(sent) == 101
        session.feed(np.full(320 * 1500, noise, np.float32))
        await session.drain_input()
        assert len(sent) == 201, 'each new utterance gets its own endpointing tail'
        assert counters['input_seconds'] == pytest.approx(4.02)
        assert counters['zero_seconds'] == pytest.approx(4)
        assert not session.failed and not session._input_silence
    finally:
        sender.cancel()
        await real_asyncio.gather(sender, return_exceptions=True)


@pytest.mark.parametrize('level,expected_silent', [(0.003, True), (1/128, False), (.02, False)])
def test_rms_gate_uses_each_complete_frame_and_preserves_threshold_boundary(level, expected_silent):
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    session = RealtimeSession(config=RealtimeConfig(silence_rms=1/128),
        player=None, on_tool=None, on_event=lambda *args: None)
    quiet = np.full(320, level, np.float32)
    speech = np.full(320, .1, np.float32)
    # A capture block may straddle frames and contain both noise and speech.
    session.feed(quiet[:123])
    assert session.input.empty()
    session.feed(np.concatenate([quiet[123:], speech]))
    first, second = session.input.get_nowait(), session.input.get_nowait()
    assert (not any(first)) == expected_silent
    assert first == (bytes(640) if expected_silent else (quiet*32768).astype('<i2').tobytes())
    assert second == (speech*32768).astype('<i2').tobytes()


@pytest.mark.parametrize('threshold', [-.001, 1.01, float('nan'), float('inf')])
def test_invalid_silence_rms_is_rejected(threshold):
    from lira.audio.doubao_realtime import RealtimeConfig
    with pytest.raises(ValueError, match='silence RMS'):
        RealtimeConfig(silence_rms=threshold)


@pytest.mark.asyncio
@pytest.mark.parametrize('stall_seconds',[0,.9])
async def test_short_scheduler_delays_do_not_accumulate_upload_clock_drift(monkeypatch,stall_seconds):
    import math
    import lira.audio.doubao_realtime as module
    clock=[1000.0]
    busy_slot=[-1]
    timestamps=[]
    real_asyncio=asyncio
    class Scheduler:
        def __getattr__(self,name):return getattr(real_asyncio,name)
        async def sleep(self,delay):
            clock[0]+=max(delay,0)
            slot=math.floor((clock[0]-1000)/.1 + 1e-6)
            if slot>busy_slot[0]:
                clock[0]+=.025  # Synchronous processing every 100 ms capture block.
                busy_slot[0]=slot
            await real_asyncio.sleep(0)
    monkeypatch.setattr(module,'asyncio',Scheduler())
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    s=module.RealtimeSession(config=module.RealtimeConfig(),player=None,
                             on_event=lambda *args:None,on_tool=None)
    s.input=real_asyncio.Queue(maxsize=100)
    async def send(*args,**kwargs):
        timestamps.append(clock[0]);clock[0]+=.001
        if len(timestamps)==50:clock[0]+=stall_seconds
    s.send=send
    s.feed(np.full(320*100,.2,np.float32))
    sender=real_asyncio.create_task(s._send_audio())
    try:
        await real_asyncio.wait_for(s.drain_input(),2)
        elapsed=timestamps[-1]-timestamps[0]
        assert elapsed<=2.02+stall_seconds, f'100 frames accumulated drift: {elapsed:.3f}s'
        assert max(sum(t<=v<t+.02 for v in timestamps) for t in timestamps)<=3
    finally:
        sender.cancel();await real_asyncio.gather(sender,return_exceptions=True)


@pytest.mark.asyncio
async def test_receiving_reply_and_control_events_does_not_wait_for_speaker():
    import base64
    from lira.audio.doubao_realtime import RealtimeSession,RealtimeConfig
    incoming=asyncio.Queue()
    for e in [{'type':'session.created'},{'type':'response.output_audio.started'},
              {'type':'response.output_audio.delta','delta':base64.b64encode(b'\0'*144000).decode()},
              {'type':'response.output_audio.done'},{'type':'response.done'}]:
        incoming.put_nowait(json.dumps(e))
    class Socket:
        async def recv(self):return await incoming.get()
        async def send(self,data):pass
        async def close(self):pass
    async def connect(*args,**kwargs):return Socket()
    release,done=asyncio.Event(),asyncio.Event()
    class Player:
        async def open(self):pass
        async def stop(self):release.set()
        async def close(self):pass
        async def play_stream(self,chunks):
            await release.wait()
            async for part in chunks:pass
    s=RealtimeSession(config=RealtimeConfig(),player=Player(),connect=connect,on_tool=None,
        on_event=lambda k,t:done.set() if k=='response_done' else None)
    # _start_output calls stop first; arm a distinct hold after that stop.
    async def stop():pass
    s.player.stop=stop
    try:
        await s.open()
        await asyncio.wait_for(done.wait(),.5)
        assert s.output_occupied and not s.failed
        assert s.output.maxsize*960<=3*1024*1024
        release.set();await asyncio.wait_for(s.playback,1)
    finally:
        release.set();await s.close()


@pytest.mark.asyncio
async def test_resuming_microphone_does_not_put_speech_behind_old_playback_silence():
    from lira.audio.doubao_realtime import RealtimeSession,RealtimeConfig
    rt,paths=runtime()
    class Player:
        async def stop(self):pass
        async def close(self):pass
    s=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *args:None,on_tool=None)
    paths.mode='doubao_realtime';paths.state='listening';paths.session=s
    paths._output_protected=True
    s.feed(np.zeros(320*25,np.float32))
    paths.feed(np.full(1600,.2,np.float32))
    assert not s.failed
    assert s.input.qsize()==5, 'old keepalive must not consume the new speech budget'
    assert not any(s._input_silence)
    await paths.close()

@pytest.mark.asyncio
async def test_live_watch_has_no_three_minute_cutoff_and_waits_after_playback(monkeypatch):
    import lira.conversation_paths as module
    _, paths = runtime()
    session = FakeSession()
    paths.session = session
    paths.state = 'listening'
    paths._last_activity = 0
    clock = SimpleNamespace(now=0)
    ticks = iter([181, 240, 299, 300])
    events = []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(module, 'safe_add_event', lambda telemetry, kind, text: events.append((kind, text)))

    async def sleep(delay):
        clock.now = next(ticks)
        if clock.now == 181:
            paths._event('user_text', '刚刚还在说话')
        session.output_occupied = clock.now == 240
        assert not session.closed

    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=sleep))
    await paths._watch(session)
    assert clock.now == 300
    assert session.closed and paths.session is None
    assert events[-1] == ('realtime_ended', '连续 60 秒无对话活动，实时语音会话已结束，可再次唤醒。')


@pytest.mark.asyncio
async def test_old_watch_does_not_stop_replacement_session(monkeypatch):
    import lira.conversation_paths as module
    _, paths = runtime()
    old, new = FakeSession(), FakeSession()
    paths.session = old
    paths._last_activity = 0
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 1000))

    async def sleep(delay):
        paths.session = new

    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=sleep))
    await paths._watch(old)
    assert paths.session is new and not new.closed

@pytest.mark.asyncio
async def test_ir_voice_prepare_learn_persist_and_control(tmp_path):
    from lira.appliances import IRService, LearningManager
    from lira.hal.mock.ir import MockIrController
    rt, paths = runtime()
    rt.store.close()
    rt.store = ApplianceStore(tmp_path / 'ir.db')
    ir = MockIrController()
    rt.ir_service = IRService(rt.store, ir)
    rt.learning = LearningManager(rt.ir_service)
    args = {'device': '客厅电视', 'action': '音量加', 'category': '电视'}
    assert (await paths._tool('start_infrared_learning', {}))['status'] == 'not_prepared'
    assert (await paths._tool('prepare_infrared_learning', args))['status'] == 'prepared'
    assert rt.store.get_appliance('客厅电视') is None  # preparation has no side effects
    ir.learn_queue.append('local-secret-code')
    assert (await paths._tool('start_infrared_learning', {}))['status'] == 'learning'
    await paths.infrared.task
    result = await paths._tool('get_infrared_learning_status', {})
    assert result['status'] == 'learned' and 'local-secret-code' not in json.dumps(result)
    assert ir.send_count == 0  # learning never sends to appliance automatically
    reopened = ApplianceStore(tmp_path / 'ir.db')
    assert reopened.get_appliance('客厅电视').codes['音量加'] == 'local-secret-code'
    reopened.close()
    listing = await paths._tool('list_infrared_devices', {})
    assert listing['devices'][0]['actions'] == [{'name': '音量加', 'learned': True}]
    assert 'local-secret-code' not in json.dumps(listing)
    assert (await paths._tool('control_infrared_device', {'device':'客厅电视', 'action':'音量加'}))['status'] == 'sent'
    assert ir.send_count == 1
    assert (await paths._tool('prepare_infrared_learning', args))['status'] == 'rejected'
    await paths.close()
    rt.store.close()


@pytest.mark.asyncio
async def test_ir_learning_can_be_cancelled_and_does_not_cancel_other_owner():
    from lira.appliances import IRService, LearningManager, ApplianceModel
    rt, paths = runtime()
    class Ir:
        async def learn(self, timeout):
            await asyncio.Event().wait()
    rt.ir_service = IRService(rt.store, Ir())
    rt.learning = LearningManager(rt.ir_service)
    args = {'device':'风扇', 'action':'开关', 'category':'风扇'}
    await paths._tool('prepare_infrared_learning', args)
    await paths._tool('start_infrared_learning', {})
    task = paths.infrared.task
    await asyncio.sleep(0)
    assert (await paths._tool('cancel_infrared_learning', {}))['status'] == 'cancelled'
    assert task.cancelled() and not rt.store.get_appliance('风扇').codes
    rt.store.upsert_appliance(ApplianceModel(name='电视'))
    other = rt.learning.begin('电视', '开关')
    await paths._tool('cancel_infrared_learning', {})
    assert not other.cancelled()
    assert (await paths._tool('prepare_infrared_learning', args))['status'] == 'busy'
    other.cancel()
    await asyncio.gather(other, return_exceptions=True)
    await paths.close()


@pytest.mark.asyncio
async def test_ir_voice_failures_privacy_high_risk_and_session_cleanup():
    from lira.appliances import IRService, LearningManager, ApplianceModel
    from lira.hal.mock.ir import MockIrController
    rt, paths = runtime()
    ir = MockIrController()
    rt.ir_service = IRService(rt.store, ir)
    rt.learning = LearningManager(rt.ir_service)
    for name, enabled, risk in [('禁用电视',False,False), ('取暖器',True,True)]:
        rt.store.upsert_appliance(ApplianceModel(name=name, enabled=enabled,
            is_high_risk=risk, actions={'开':()}, codes={'开':'code'}))
        result = await paths._tool('control_infrared_device', {'device':name,'action':'开'})
        assert result['status'] == 'rejected'
    assert ir.send_count == 0
    args={'device':'新电视','action':'开关','category':'电视'}
    await paths._tool('prepare_infrared_learning', args)
    await paths._tool('start_infrared_learning', {})
    await asyncio.sleep(.02)
    assert (await paths._tool('get_infrared_learning_status', {}))['status'] == 'failed'
    assert not rt.store.get_appliance('新电视').codes
    await paths._tool('prepare_infrared_learning', args)
    await paths._tool('start_infrared_learning', {})
    task = paths.infrared.task
    await rt.privacy.set_enabled(True, source='test')
    with pytest.raises(PermissionError):
        await paths._tool('control_infrared_device', {'device':'取暖器','action':'开'})
    await paths.apply_privacy(True)
    assert task.cancelled() and paths.infrared.pending is None
    await paths.close()


def test_ir_tool_arguments_are_validated_before_dispatch():
    from lira.audio.doubao_realtime import TOOLS
    from lira.infrared_voice import validate_tool_arguments
    good={'device':'电视', 'action':'开', 'category':'电视'}
    validate_tool_arguments('prepare_infrared_learning', good, TOOLS)
    for bad in [None, [], {}, dict(good, extra=True), dict(good, device=1),
                dict(good, device=' '), dict(good, category='随便'), dict(good, action='x'*41)]:
        with pytest.raises(ValueError):
            validate_tool_arguments('prepare_infrared_learning', bad, TOOLS)
    with pytest.raises(ValueError):
        validate_tool_arguments('start_voice_enrollment', {'extra':True}, TOOLS)

@pytest.mark.asyncio
async def test_ir_function_call_wire_dispatch_and_replay_deduplication():
    from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession
    from lira.appliances import IRService, LearningManager, ApplianceModel
    from lira.hal.mock.ir import MockIrController
    rt, paths = runtime()
    ir = MockIrController()
    rt.ir_service = IRService(rt.store, ir)
    rt.learning = LearningManager(rt.ir_service)
    rt.store.upsert_appliance(ApplianceModel(name='电视', actions={'音量加':()}, codes={'音量加':'secret-local-code'}))
    sent=[]
    class Socket:
        async def send(self, value): sent.append(json.loads(value))
        async def close(self): pass
    class Player:
        async def stop(self): pass
        async def close(self): pass
    session=RealtimeSession(config=RealtimeConfig(), player=Player(), on_event=lambda *a:None, on_tool=paths._tool)
    session.ws=Socket()
    event={'items':[{'call_id':'ir-1', 'name':'control_infrared_device',
        'arguments':json.dumps({'device':'电视','action':'音量加'})}]}
    await session._tool(event)
    await session._tool(event)
    assert ir.send_count == 1
    assert json.loads(sent[-1]['items'][0]['content'][0]['text'])['status'] == 'sent'
    assert 'secret-local-code' not in json.dumps(sent)
    await session._tool({'items':[{'call_id':'ir-invalid', 'name':'control_infrared_device',
        'arguments':json.dumps({'device':'电视','action':'音量加','confirmed':True})}]})
    assert ir.send_count == 1
    assert json.loads(sent[-1]['items'][0]['content'][0]['text'])['status'] == 'rejected'
    await session.close()
    await paths.close()

@pytest.mark.asyncio
async def test_agent_task_tools_realtime_only_and_stop_invalidates():
    rt, paths=runtime()
    assert 'activate_agent_task' in paths.capabilities.entries
    await paths.switch('doubao_realtime')
    paths.capabilities_enabled=True
    await paths.start()
    assert paths.session.kwargs['capabilities'] is paths.capabilities
    paths._event('user_text','等我写完再继续')
    assert paths.agent_task.turn['text']=='等我写完再继续'
    paths._event('assistant_text','好的')
    assert paths.agent_task.turn['text']=='等我写完再继续'
    await paths.stop()
    assert paths.agent_task.generation is None
    await paths.switch('classic')
    assert paths.agent_task.snapshot()['agreement'] is None
    await paths.close()

@pytest.mark.asyncio
async def test_confirmed_voice_track_change_revokes_task_without_epoch_change():
    rt, paths=runtime()
    await paths.switch('doubao_realtime')
    paths.capabilities_enabled=True
    await paths.start()
    old_epoch=paths.speaker_input.epoch
    old_generation=paths.agent_task.generation
    paths.agent_task.observe_user('自己说下一项')
    await paths.agent_task.call('propose_agent_task',{'revision':0,'terms':'自己说下一项'})
    paths.speaker_input.track_generation=getattr(paths.speaker_input,'track_generation',0)+1
    await paths._publish_identity(paths.speaker_input.context())
    assert paths.speaker_input.epoch==old_epoch
    assert paths.agent_task.generation!=old_generation
    assert paths.agent_task.snapshot()['proposal'] is None
    await paths.close()

@pytest.mark.asyncio
async def test_owner_change_retires_cloud_context_and_fences_old_callbacks():
    rt, paths = runtime()
    paths.capabilities_enabled = True
    await paths.switch('doubao_realtime')
    await paths.start()
    old = paths.session
    old.kwargs['on_event']('user_text', 'old private content')
    paths.speaker_input.track_generation += 1
    await paths._publish_identity(paths.speaker_input.context())
    assert old.closed and paths.session is not old
    assert paths.session.frames == []
    old.kwargs['on_event']('user_text', 'late old private result')
    old.kwargs['on_event']('error', 'late old failure')
    assert paths.agent_task.turn is None
    assert paths.state == 'listening'
    assert await old.kwargs['on_tool']('identify_current_speaker', {}) == {'status':'cancelled'}
    assert paths._recent_conversation() == {'status':'unavailable','reason':'owner_context_changed','turns':[]}
    await paths.close()

@pytest.mark.asyncio
async def test_agreed_pending_timer_outlives_idle_but_pause_restores_idle(monkeypatch):
    import lira.conversation_paths as module
    rt, paths = runtime()
    paths.capabilities_enabled = True
    await paths.switch('doubao_realtime')
    await paths.start()
    async def verify(*args): return True
    paths.agent_task.verifier = verify
    paths.agent_task.observe_user('我同意计时两分钟')
    await paths.agent_task.call('propose_agent_task', {'revision':0,'terms':'计时两分钟'})
    await paths.agent_task.call('activate_agent_task', {'revision':1,'turn_id':paths.agent_task.turn['id']})
    await paths.agent_timers.call('create_task_timer', {'revision':2,'duration_seconds':120})
    now = paths._last_activity + 61
    monkeypatch.setattr(module.time, 'monotonic', lambda:now)
    session = paths.session
    assert not await paths._stop_if_idle(session)
    assert not session.closed
    await paths.agent_task.call('pause_agent_task', {'revision':2})
    now += 61
    assert await paths._stop_if_idle(session)
    assert session.closed
    await paths.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'failure', 'privacy'])
async def test_owner_reconnect_seeds_only_new_identity_and_fails_closed(outcome):
    rt, paths = runtime()
    sessions = []
    opening, release = asyncio.Event(), asyncio.Event()
    class Session(FakeSession):
        async def open(self):
            if sessions:
                assert await self.kwargs['on_tool']('identify_current_speaker', {}) == {'status':'cancelled'}
                self.kwargs['on_event']('user_text','unsolicited before identity seed')
                assert paths.agent_task.turn is None
                opening.set()
                await release.wait()
                if outcome == 'failure':raise RuntimeError('provider failed')
            sessions.append(self)
        async def set_identity_context(self, context):self.identity = context.copy()
    paths._session_factory = Session
    paths.capabilities_enabled = True
    await paths.switch('doubao_realtime')
    await paths.start()
    old = paths.session
    epoch = paths.speaker_input.epoch
    paths.speaker_input.context = lambda:{'status':'recognized','name':'new child','address':'new child'}
    paths.speaker_input.track_generation += 1
    rotation = asyncio.create_task(paths._publish_identity(paths.speaker_input.context()))
    await opening.wait()
    assert old.closed and paths.session is None
    paths.feed(np.ones(160, np.float32))
    assert old.frames == []
    if outcome == 'privacy':await rt.privacy.set_enabled(True, source='test')
    release.set()
    await asyncio.wait_for(rotation, 1)
    assert paths.speaker_input.epoch == epoch
    if outcome == 'success':
        assert paths.session.identity['name'] == 'new child'
        assert paths.session.frames == []
        assert paths.session.kwargs.get('messages') is None
    else:
        assert paths.session is None and paths.agent_task.generation is None
        assert paths.state == ('standby' if outcome == 'privacy' else 'error')
        if outcome == 'privacy':assert sessions[-1].closed
    await paths.close()

@pytest.mark.asyncio
async def test_first_unknown_capability_session_cannot_fetch_previous_owner_history():
    rt, paths = runtime()
    paths.history.begin()
    paths.history.event('user_text', 'previous private content')
    paths.history.end()
    paths.capabilities_enabled = True
    await paths.switch('doubao_realtime')
    await paths.start()
    result = await paths.session.kwargs['on_tool']('get_recent_device_conversation', {})
    assert result == {'status':'unavailable','reason':'previous_owner_unverified','turns':[]}
    assert 'previous private content' not in json.dumps(result)
    await paths.close()

@pytest.mark.asyncio
async def test_explicit_member_correction_replaces_cloud_without_track_change():
    rt, paths = runtime()
    paths.capabilities_enabled = True
    await paths.switch('doubao_realtime')
    await paths.start()
    old = paths.session
    track = paths.speaker_input.track_generation
    paths.speaker_input.state = 'recognized'
    paths.speaker_input.member_id = 99
    await paths._publish_identity({'status':'recognized','name':'new','address':'new'})
    assert old.closed and paths.session is not old
    assert paths.speaker_input.track_generation == track
    await paths.close()

@pytest.mark.asyncio
async def test_flag_off_identity_change_keeps_existing_cloud_context():
    rt, paths = runtime()
    await paths.switch('doubao_realtime')
    await paths.start()
    old = paths.session
    paths._agent_owner_epoch = paths._owner_stamp()
    paths.speaker_input.track_generation += 1
    await paths._publish_identity(paths.speaker_input.context())
    assert paths.session is old and not old.closed
    await paths.close()

@pytest.mark.asyncio
async def test_pending_agreed_wait_protects_idle_without_terminal_keepalive(monkeypatch):
    import lira.conversation_paths as module
    from lira.realtime_capabilities import CapabilityExecutor
    rt, paths = runtime()
    paths.capabilities_enabled = True
    await paths.switch('doubao_realtime')
    await paths.start()
    async def verify(*args):return True
    paths.agent_task.verifier = verify
    paths.agent_task.observe_user('我同意等待')
    await paths.agent_task.call('propose_agent_task',{'revision':0,'terms':'等待'})
    await paths.agent_task.call('activate_agent_task',{'revision':1,'turn_id':paths.agent_task.turn['id']})
    executor = CapabilityExecutor(paths.capabilities)
    paths.session.capabilities = executor
    pending = asyncio.create_task(executor.call('wait_for_task_event',{'revision':2,'timeout_seconds':40},'wait'))
    for _ in range(4):await asyncio.sleep(0)
    now = paths._last_activity + 61
    monkeypatch.setattr(module.time,'monotonic',lambda:now)
    assert not await paths._stop_if_idle(paths.session)
    pending.cancel()
    await asyncio.gather(pending,return_exceptions=True)
    await executor.close()
    now += 61
    assert await paths._stop_if_idle(paths.session)
    await paths.close()

@pytest.mark.asyncio
async def test_owner_rotation_from_old_tool_task_cannot_cancel_reconnect():
    from lira.audio.doubao_realtime import RealtimeSession
    rt, paths = runtime()
    class Player:
        async def stop(self):pass
        async def close(self):pass
    class Session(RealtimeSession):
        async def open(self):pass
        async def set_identity_context(self, context):return True
    paths._session_factory = lambda **kwargs:Session(player=Player(), **kwargs)
    paths.capabilities_enabled = True
    await paths.switch('doubao_realtime')
    await paths.start()
    old = paths.session
    paths.speaker_input.track_generation += 1
    caller = asyncio.create_task(paths._publish_identity(paths.speaker_input.context()))
    old._tool_tasks.add(caller)
    await asyncio.gather(caller, return_exceptions=True)
    for _ in range(100):
        if paths.session is not None:break
        await asyncio.sleep(.001)
    assert old.closed and paths.session is not None and paths.session is not old
    assert paths.state == 'listening'
    await paths.close()

@pytest.mark.asyncio
async def test_owner_change_replaces_real_executor_and_cancels_pending_operation():
    from lira.audio.doubao_realtime import RealtimeSession
    rt, paths=runtime()
    class Player:
        async def stop(self):pass
        async def close(self):pass
    entered,cancelled=asyncio.Event(),asyncio.Event()
    async def slow(args):
        entered.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    paths.capabilities.register('owner_slow','test',{'type':'object','properties':{}},slow,background=True)
    paths.capabilities_enabled=True
    class LocalSession(RealtimeSession):
        async def open(self):pass
        async def set_identity_context(self, context):return True
    paths._session_factory=lambda **kwargs: LocalSession(player=Player(),**kwargs)
    # Avoid network open: the session itself and executor are real, transport is absent.
    paths.agent_task.begin('task-generation','owner')
    paths.session=paths._make_session()
    paths.mode='doubao_realtime'
    old=paths.session.capabilities
    op=await old.call('owner_slow',{},'slow')
    await entered.wait()
    paths.speaker_input.track_generation+=1
    await paths._publish_identity(paths.speaker_input.context())
    assert old.closed and cancelled.is_set()
    assert paths.session.capabilities is not old
    result=await paths.session.capabilities.call('get_capability_operation',{'operation_id':op['operation_id']},'read')
    assert result['status']=='unknown'
    assert (await paths.session.capabilities.call('read_agent_task',{},'read-task'))['status']=='ok'
    await paths.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['epoch','track'])
async def test_capability_owner_change_revokes_tv_ownership_and_pending_without_clearing(change):
    rt,paths=runtime()
    await paths.switch('doubao_realtime')
    paths.capabilities_enabled=True
    await paths.start()
    calls=[]
    class Sender:
        async def show(self,payload):calls.append(('show',payload['id']))
        async def clear(self,content_id):calls.append(('clear',content_id))
    paths.tv.sender=Sender()
    paths.tv.displayed_id='old-child-content'
    prepared=await paths.tv.call('prepare_tv_text',{'text':'old pending'})
    paths.tv.event('assistant_text',prepared['question'])
    paths.tv.event('response_done','')
    paths.tv.event('user_text','可以')
    if change=='track':paths.speaker_input.track_generation+=1
    else:paths.speaker_input.epoch+=1
    await paths._publish_identity(paths.speaker_input.context())
    assert paths.tv.pending is None
    assert (await paths.tv._clear({'content_id':'old-child-content'}))['status']=='failed'
    assert (await paths.tv.call('confirm_tv_display',{'request_id':prepared['request_id']}))['status']=='confirmation_required'
    assert calls==[]
    await paths.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',['privacy','stop','close'])
async def test_realtime_timer_wait_revoked_by_real_paths_lifecycle(revoke):
    rt,paths=runtime()
    async def verify(*args):return True
    paths.agent_task.verifier=verify
    paths.agent_task.begin('session','child')
    paths.agent_task.observe_user('我同意使用计时')
    await paths.agent_task.call('propose_agent_task',{'revision':0,'terms':'允许计时'})
    await paths.agent_task.call('activate_agent_task',{'revision':1,'turn_id':paths.agent_task.turn['id']})
    handler=paths.capabilities.entries['create_task_timer'].handler
    await handler({'revision':2,'duration_seconds':20})
    waiting=asyncio.create_task(paths.capabilities.entries['wait_for_task_event'].handler({'revision':2,'timeout_seconds':30}))
    await asyncio.sleep(0)
    if revoke=='privacy':await paths.apply_privacy(True)
    elif revoke=='stop':await paths.stop()
    else:await paths.close()
    assert (await asyncio.wait_for(waiting,.1))['status']=='cancelled'
    assert not paths.agent_timers.timers
    await paths.close()


@pytest.mark.asyncio
async def test_tv_audio_is_realtime_only_and_uses_paired_sender(monkeypatch):
    from lira.tv_audio import TvAudioPlayer
    rt, paths = runtime()
    paths.audio_output = 'tv'
    paths._session_factory = None
    paths.tv.sender = object()
    rt.settings = SimpleNamespace(tts_speed=1.0, volume=1.0)
    monkeypatch.setattr('lira.conversation_paths.RealtimeSession', FakeSession)
    session = paths._make_session()
    assert isinstance(session.kwargs['player'], TvAudioPlayer)
    assert session.kwargs['player'].sender is paths.tv.sender
    # Classic still returns samples to its original consumer, no TV session.
    assert paths.mode == 'classic'
    assert paths.feed(np.zeros(320, np.float32)) is False
    await paths.close()
