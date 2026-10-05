import asyncio
import pytest
from lira.dialog import DialogEngine, Router, State
from lira.dialog.context import ContextSnapshot, ConversationPair
from test_state_machine import Recorder

async def test_fragment_requires_local_confirmation():
    calls=[]
    async def remote(text):
        calls.append(text)
        return '回答'
    cb=Recorder(); engine=DialogEngine(Router(remote_llm=remote, remote_available=lambda:True),cb)
    await engine.on_wake(); await engine.on_asr_text('这本书')
    assert engine.state.value == 'clarifying'
    assert cb.captures == 0 and calls == []
    await engine.on_asr_text('是的')
    assert engine.state is State.CAPTURING and engine.read_original and cb.captures == 1

async def test_followup_has_completed_pair_and_expiry_clears():
    seen=[]
    async def remote(text, *, context=None):
        seen.append(context)
        return '回答'+text
    engine=DialogEngine(Router(remote_llm=remote, remote_available=lambda:True),Recorder())
    await engine.on_wake(); await engine.on_asr_text('问题'); await engine.on_asr_text('为什么')
    assert seen[1].pairs == (ConversationPair('问题','回答问题'),)
    engine.tick(16); await engine.on_wake(); await engine.on_asr_text('新问题')
    assert seen[-1].pairs == ()

async def test_router_carries_snapshot_without_mutating_shared_handler():
    seen=[]
    async def handler(text, *, context=None, on_wait_feedback=None):
        await asyncio.sleep(0)
        on_wait_feedback()
        seen.append((text,context))
        return text
    router=Router(remote_llm=handler, remote_available=lambda:True)
    a=ContextSnapshot((ConversationPair('甲','答甲'),)); b=ContextSnapshot((ConversationPair('乙','答乙'),))
    feedback=[]
    await asyncio.gather(router.route('a',context=a,on_wait_feedback=lambda:feedback.append('a')),router.route('b',context=b,on_wait_feedback=lambda:feedback.append('b')))
    assert seen == [('a',a),('b',b)] and feedback == ['a','b']

class DelayedRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.handles=[]
    def speak(self,text,*,owner=None):
        from lira.audio.tts import PlaybackCompletion
        super().speak(text)
        event=PlaybackCompletion(owner=owner)
        self.handles.append(event)
        return event

async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)

async def test_clarification_deadline_begins_after_play_and_onset_preserved():
    cb=DelayedRecorder(); engine=DialogEngine(Router(),cb)
    await engine.on_wake(); await engine.on_asr_text('这本书')
    engine.tick(100)
    assert engine.state is State.CLARIFYING
    cb.handles[-1].status='success'; cb.handles[-1].set(); await settle()
    engine.tick(114)
    engine.on_speech_started(engine.listening_generation)
    engine.tick(116)
    assert engine.state is State.CLARIFYING
    await engine.on_asr_text('是')
    assert cb.captures == 1

async def test_clarification_empty_endpoint_and_expiry_clear_round():
    cb=DelayedRecorder(); engine=DialogEngine(Router(),cb)
    await engine.on_wake(); await engine.on_asr_text('这本书')
    cb.handles[-1].status='success'; cb.handles[-1].set(); await settle()
    engine.on_speech_started(engine.listening_generation)
    engine.on_empty_endpoint(engine.listening_generation)
    engine.tick(15)
    assert engine.state is State.STANDBY and engine._pending_reading is None

@pytest.mark.parametrize('answer',['不是','不用','不是的'])
async def test_negative_does_not_capture_or_leave_pending(answer):
    cb=Recorder(); engine=DialogEngine(Router(),cb)
    await engine.on_wake(); await engine.on_asr_text('这本书'); await engine.on_asr_text(answer)
    assert cb.captures == 0 and engine._pending_reading is None
    assert engine.state is State.LISTENING

async def test_full_command_replaces_clarification():
    cb=Recorder(); engine=DialogEngine(Router(),cb)
    await engine.on_wake(); await engine.on_asr_text('这本书'); await engine.on_asr_text('帮我读下这封信')
    assert cb.captures == 1 and not engine.read_original
    assert engine._pending_reading is None

async def test_cancelled_answer_not_in_completed_context():
    cb=DelayedRecorder(); seen=[]
    async def remote(text,*,context):
        seen.append(context)
        return '尚未播完'
    engine=DialogEngine(Router(remote_llm=remote,remote_available=lambda:True),cb)
    await engine.on_wake(); await engine.on_asr_text('问题')
    await engine.on_wake(); await engine.on_asr_text('另一个问题')
    assert seen[-1].pairs == ()
    engine.close(); await settle()

async def test_remote_messages_project_only_typed_task_facts():
    import json
    from test_llm_client import FakeRemote, ok_response, make_client
    from lira.dialog.context import ReadingTaskFacts, ReadingTaskStatus
    from lira.llm import make_remote_handler
    fake=FakeRemote(ok_response('回答'))
    handler=make_remote_handler(make_client(fake))
    context=ContextSnapshot((ConversationPair('问题','答案'),),ReadingTaskFacts(ReadingTaskStatus.PAUSED,True))
    await handler('为什么',context=context)
    body=json.loads(fake.last_request.read().decode('utf-8'))
    assert body['messages'][-3:] == [
        {'role':'user','content':'问题'},
        {'role':'assistant','content':'答案'},
        {'role':'user','content':'为什么'},
    ]
    facts=body['messages'][1]['content']
    assert 'paused' in facts and 'resumable' in facts
    assert 'OCR' not in facts

async def test_failed_answer_excluded_from_history():
    cb=DelayedRecorder(); seen=[]
    async def remote(text,*,context):
        seen.append(context)
        return '失败回答'
    engine=DialogEngine(Router(remote_llm=remote,remote_available=lambda:True),cb)
    await engine.on_wake(); await engine.on_asr_text('问题')
    cb.handles[-1].status='failed'; cb.handles[-1].set(); await settle()
    await engine.on_asr_text('为什么')
    assert seen[-1].pairs == ()
    engine.close(); await settle()
