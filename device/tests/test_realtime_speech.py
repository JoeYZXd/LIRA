import base64
import asyncio
import json

import pytest

from lira.audio.doubao_realtime import RealtimeConfig, RealtimeSession


@pytest.mark.asyncio
async def test_interrupt_after_audio_done_never_commits_completed_history():
    incoming, release, sent = asyncio.Queue(), asyncio.Event(), []
    class Socket:
        async def send(self, data): sent.append(json.loads(data))
        async def recv(self): return await incoming.get()
        async def close(self): pass
    class Player:
        async def play_stream(self, chunks):
            async for _ in chunks: pass
            await release.wait()
        async def stop(self): pass
        async def close(self): pass
    session = RealtimeSession(config=RealtimeConfig(), player=Player(), on_event=lambda *a:None, on_tool=None)
    session.ws = Socket()
    receiver = asyncio.create_task(session._receive())
    speech = asyncio.create_task(session.speak_text('青色盒子'))
    await asyncio.sleep(.01)
    for kind in ('response.output_audio.started', 'response.output_audio.done'):
        incoming.put_nowait(json.dumps({'type':kind}))
    await asyncio.sleep(.02)
    await session.interrupt()
    try:
        assert await asyncio.wait_for(speech, 1) is False
        assert not any(row['type']=='conversation.item.create' for row in sent)
    finally:
        release.set(); receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await session.close()

from contextlib import asynccontextmanager
from types import SimpleNamespace
from lira.agent_task_state import AgentTaskState
from lira.realtime_capabilities import CapabilityRegistry
from lira.realtime_speech import RealtimeSpeech


@asynccontextmanager
async def harness(tool_send_gate=None):
    incoming, release, sent = asyncio.Queue(), asyncio.Event(), []
    class Socket:
        async def send(self, data):
            value=json.loads(data)
            if (tool_send_gate is not None and value['type']=='conversation.item.create'
                    and value['items'][0].get('role')=='tool'):
                await tool_send_gate.wait()
            sent.append(value)
        async def recv(self): return await incoming.get()
        async def close(self): pass
    class Player:
        async def play_stream(self, chunks):
            async for _ in chunks: pass
            await release.wait()
        async def stop(self): pass
        async def close(self): pass
    state = AgentTaskState(); state.begin('session', 'child')
    rt = SimpleNamespace(privacy=SimpleNamespace(is_on=False))
    registry = CapabilityRegistry()
    session = None
    service = RealtimeSpeech(rt, lambda:session, state); service.register(registry)
    session = RealtimeSession(config=RealtimeConfig(), player=Player(), on_event=lambda *a:None,
                              on_tool=None, capabilities=registry)
    session.ws = Socket()
    receiver = asyncio.create_task(session._receive())
    def emit(kind, **fields): incoming.put_nowait(json.dumps({'type':kind, **fields}))
    async def until(predicate):
        for _ in range(120):
            if predicate():return
            await asyncio.sleep(.01)
        raise AssertionError('condition did not occur')
    try:yield session, state, rt, sent, release, emit, until
    finally:
        release.set(); receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await session.close()


@pytest.mark.asyncio
async def test_pending_tool_response_barrier_and_physical_drain_and_deduplication():
    async with harness() as (s, state, rt, sent, release, emit, until):
        event = {'items':[{'name':'start_exact_speech','call_id':'same',
                          'arguments':json.dumps({'text':'青色盒子'})}]}
        tool = asyncio.create_task(s._tool(event))
        await asyncio.sleep(.04)
        assert not any(e['type']=='speech_text_buffer.commit' for e in sent)
        assert tool.done()
        assert any(e['type']=='conversation.item.create' and e['items'][0].get('role')=='tool' for e in sent)
        initial=json.loads(next(e for e in sent if e['type']=='conversation.item.create')['items'][0]['content'][0]['text'])
        assert initial['status']=='pending'
        assert initial['initial_playback_state']=='queued_not_started'
        assert initial['completion_confirmed'] is False
        assert initial['result_query']=='get_capability_operation'
        assert not s._speech_barriers['same']['ready']
        emit('response.done')
        await until(lambda:any(e['type']=='speech_text_buffer.commit' for e in sent))
        assert await s.speak_text('身份插话') is False
        emit('response.output_audio.started');emit('response.output_audio.delta',delta=base64.b64encode(b'\x01\x00'*480).decode());emit('response.output_audio.done')
        await asyncio.sleep(.05)
        op=next(iter(s.capabilities.operations.values()))
        assert op['status']=='pending'
        release.set();await asyncio.wait_for(op['task'],2)
        assert op['result']['status']=='completed'
        await s._tool(event)
        assert sum(e['type']=='speech_text_buffer.commit' for e in sent)==1
        assert len(s.capabilities.operations)==1
        replay=json.loads([e for e in sent if e['type']=='conversation.item.create'][-1]['items'][0]['content'][0]['text'])
        assert replay==initial  # explicitly an initial snapshot, not current playback state


@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['pause','privacy','input','collision','cancel','close'])
async def test_active_speech_is_revoked_without_completed_history(reason):
    async with harness() as (s, state, rt, sent, release, emit, until):
        state.phase='active'
        tool=asyncio.create_task(s._tool({'items':[{'name':'start_exact_speech','call_id':'one',
                    'arguments':json.dumps({'text':'测试文本'})}]}))
        await asyncio.sleep(.03);emit('response.done')
        await until(lambda:any(e['type']=='speech_text_buffer.commit' for e in sent))
        emit('response.output_audio.started');emit('response.output_audio.delta',delta=base64.b64encode(b'\x01\x00'*480).decode());emit('response.output_audio.done')
        await asyncio.sleep(.03)
        if reason=='pause':await state.call('pause_agent_task', {'revision':state.revision})
        elif reason=='privacy':rt.privacy.is_on=True
        elif reason=='input':emit('conversation.item.input_audio_transcription.started')
        elif reason=='collision':emit('response.output_text.done',text='普通回复')
        elif reason=='cancel':
            op=next(iter(s.capabilities.operations))
            await s.capabilities.call('cancel_capability_operation',{'operation_id':op},'cancel')
        else:await s.close()
        await asyncio.wait_for(asyncio.gather(tool,return_exceptions=True),2)
        if reason!='close':
            await asyncio.wait_for(asyncio.gather(next(iter(s.capabilities.operations.values()))['task'],return_exceptions=True),2)
        assert not any(e['type']=='conversation.item.create' and e.get('items',[{}])[0].get('role')=='assistant' for e in sent)
        if reason not in ('cancel','close'):
            assert next(iter(s.capabilities.operations.values()))['result']['status']=='interrupted'

@pytest.mark.asyncio
async def test_response_barrier_timeout_is_honest_and_new_input_keeps_receiver_alive():
    async with harness() as (s, state, rt, sent, release, emit, until):
        s._response_busy=True
        assert await s.wait_speech_slot(timeout=.03) is False
        assert not any(e['type']=='speech_text_buffer.commit' for e in sent)
        emit('conversation.item.input_audio_transcription.started')
        await until(lambda:s._input_revision==1)
        emit('response.done')
        assert await s.wait_speech_slot(timeout=.5) is True


@pytest.mark.asyncio
async def test_failed_playback_cannot_produce_successful_assistant_history():
    async with harness() as (s, state, rt, sent, release, emit, until):
        async def broken(chunks):
            async for _ in chunks: pass
            raise RuntimeError('speaker failed')
        s.player.play_stream=broken
        speech=asyncio.create_task(s.speak_text('逐字文本'))
        await until(lambda:any(e['type']=='speech_text_buffer.commit' for e in sent))
        emit('response.output_audio.started');emit('response.output_audio.delta',delta=base64.b64encode(b'\x01\x00'*480).decode());emit('response.output_audio.done')
        assert await asyncio.wait_for(speech,2) is False
        assert not any(e['type']=='conversation.item.create' for e in sent)

@pytest.mark.asyncio
async def test_early_response_done_before_actual_pending_send_does_not_open_barrier():
    gate=asyncio.Event()
    async with harness(gate) as (s,state,rt,sent,release,emit,until):
        tool=asyncio.create_task(s._tool({'items':[{'name':'start_exact_speech','call_id':'gated',
                    'arguments':json.dumps({'text':'测试'})}]}))
        await until(lambda:bool(s.capabilities.operations))
        emit('response.done')
        await asyncio.sleep(.05)
        gate.set();await tool
        await asyncio.sleep(.05)
        assert not any(e['type']=='speech_text_buffer.commit' for e in sent)
        emit('response.done')
        await until(lambda:any(e['type']=='speech_text_buffer.commit' for e in sent))
        emit('response.output_audio.started');emit('response.output_audio.delta',delta=base64.b64encode(b'\x01\x00'*480).decode());emit('response.output_audio.done')
        release.set()
        op=next(iter(s.capabilities.operations.values()))
        await asyncio.wait_for(op['task'],2)
        assert op['result']['status']=='completed'

@pytest.mark.asyncio
async def test_zero_pcm_cannot_complete_exact_speech_or_write_spoken_history():
    async with harness() as (s,state,rt,sent,release,emit,until):
        speech=asyncio.create_task(s.speak_text('没有音频不能算已讲明'))
        await until(lambda:any(e['type']=='speech_text_buffer.commit' for e in sent))
        emit('response.output_audio.started');emit('response.output_audio.done')
        release.set()
        assert await asyncio.wait_for(speech,2) is False
        assert not any(e['type']=='conversation.item.create' for e in sent)


@pytest.mark.asyncio
async def test_zero_pcm_normal_reply_cannot_publish_agreement_delivery():
    async with harness() as (s,state,rt,sent,release,emit,until):
        events=[];s.on_event=lambda *event:events.append(event)
        emit('response.output_audio.started')
        emit('response.output_text.done',text='没有音频不能算已讲明')
        emit('response.output_audio.done');release.set()
        await until(lambda:s.playback is not None and s.playback.done())
        assert not any(kind=='assistant_delivered' for kind,_ in events)

@pytest.mark.asyncio
async def test_nonzero_pcm_normal_reply_requires_actual_player_drain_for_delivery():
    async with harness() as (s,state,rt,sent,release,emit,until):
        events=[];s.on_event=lambda *event:events.append(event)
        emit('response.output_audio.started')
        emit('response.output_text.done',text='真实播出的条款')
        emit('response.output_audio.delta',delta=base64.b64encode(b'\x01\x00'*480).decode())
        emit('response.output_audio.done')
        await until(lambda:s._output_evidence is not None and s._output_evidence['consumed_bytes']==960)
        assert not any(kind=='assistant_delivered' for kind,_ in events)
        release.set()
        await until(lambda:any(kind=='assistant_delivered' for kind,_ in events))
        assert s._output_evidence['queued_bytes']==960
        assert s.playback.done() and not s.playback.cancelled()


@pytest.mark.asyncio
async def test_previous_normal_reply_pcm_cannot_credit_zero_pcm_insertion():
    async with harness() as (s,state,rt,sent,release,emit,until):
        emit('response.output_audio.started')
        emit('response.output_audio.delta',delta=base64.b64encode(b'\x01\x00'*480).decode())
        emit('response.output_audio.done');emit('response.done');release.set()
        await until(lambda:s.playback is not None and s.playback.done())
        old_evidence=s._output_evidence
        assert await s.wait_speech_slot(timeout=1)
        speech=asyncio.create_task(s.speak_text('另一段没有音频'))
        await until(lambda:any(e['type']=='speech_text_buffer.commit' for e in sent))
        emit('response.output_audio.started');emit('response.output_audio.done')
        assert await asyncio.wait_for(speech,2) is False
        assert old_evidence['consumed_bytes']==960
        assert s._output_evidence is not old_evidence
        assert s._output_evidence['consumed_bytes']==0
        assert not any(e['type']=='conversation.item.create' for e in sent)

@pytest.mark.asyncio
async def test_player_return_before_stream_end_cannot_publish_agreement():
    async with harness() as (s,state,rt,sent,release,emit,until):
        events=[];s.on_event=lambda *event:events.append(event)
        async def early_return(chunks):
            async for _ in chunks:return
        s.player.play_stream=early_return
        emit('response.output_audio.started')
        emit('response.output_text.done',text='尚未结束的音频')
        emit('response.output_audio.delta',delta=base64.b64encode(b'\x01\x00'*480).decode())
        await until(lambda:s.playback is not None and s.playback.done())
        assert not any(kind=='assistant_delivered' for kind,_ in events)
        assert not s._output_evidence['ended']
