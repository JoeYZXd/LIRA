import asyncio
from types import SimpleNamespace

import pytest

from lira.dictation_classic import ClassicDictation
from test_dictation_service import service


def runtime():
    return SimpleNamespace(privacy=SimpleNamespace(is_on=False), voice_member_id=1,
                           conversation_paths=SimpleNamespace(mode='classic'))


def test_ordinary_chat_and_unknown_speaker_do_not_enter_dictation():
    adapter = ClassicDictation.__new__(ClassicDictation)
    adapter.rt = runtime()
    adapter.preparation = None
    adapter.tools = SimpleNamespace(coordinator=SimpleNamespace(status=lambda: None))
    assert not adapter.accepts('今天天气怎么样')
    assert adapter.accepts('帮我默写第二课')
    adapter.rt.voice_member_id = None
    assert not adapter.accepts('帮我默写第二课')


@pytest.mark.asyncio
async def test_adopting_another_task_retires_old_dialogue_and_pinned_skill():
    adapter = ClassicDictation.__new__(ClassicDictation)
    adapter.rt = runtime()
    adapter.service = SimpleNamespace(owner_epoch=lambda member: 0)
    adapter._member, adapter._member_epoch = 1, 0
    adapter.preparation, adapter.dialogue = object(), object()
    coordinator = SimpleNamespace(task_id='old-task')
    def adopt():
        coordinator.task_id = 'new-task'
    adapter.tools = SimpleNamespace(coordinator=coordinator, adopt_active=adopt)
    assert not await adapter.handle('退出')
    assert adapter.dialogue is None
    assert adapter.preparation is None


@pytest.mark.asyncio
async def test_queue_submission_is_not_audio_drain():
    adapter = ClassicDictation.__new__(ClassicDictation)
    adapter.rt = runtime()
    done = asyncio.Event()
    done.status = 'pending'
    owner = object()
    engine = SimpleNamespace(_disarm=lambda: None, _transition=lambda state: None,
                             is_current=lambda candidate: candidate is owner)
    adapter.rt.engine = engine
    adapter.rt.callbacks = SimpleNamespace(speak=lambda text, owner: done)
    adapter._operation = owner
    adapter._member = 1
    adapter._completions = []
    pending = asyncio.create_task(adapter.speak('这是清水的清'))
    await asyncio.sleep(0)
    assert not pending.done()
    done.status = 'success'
    done.set()
    assert await pending is True


@pytest.mark.asyncio
async def test_missing_completion_cannot_claim_drain():
    adapter = ClassicDictation.__new__(ClassicDictation)
    adapter.rt = runtime()
    adapter.rt.engine = SimpleNamespace(_disarm=lambda: None, _transition=lambda state: None,
                                       is_current=lambda owner: True)
    adapter.rt.callbacks = SimpleNamespace(speak=lambda *args, **kwargs: None)
    adapter._operation = object()
    adapter._member = 1
    adapter._completions = []
    assert await adapter.speak('解释') is False


@pytest.mark.asyncio
async def test_trusted_classic_clarification_uses_configured_kimi_and_same_session(service):
    from openai.types.chat import ChatCompletionMessage
    from lira.dictation_tools import DictationTools
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(
            role='assistant', content='请问使用哪本语文课本？'))])

    rt = runtime()
    rt._llm = SimpleNamespace(
        _client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        _model='kimi-k3', _voice_options=lambda voice: {'temperature': .2})
    current = [None]
    def operation():
        current[0] = object()
        return current[0]
    rt.engine = SimpleNamespace(note_utterance=lambda value: None,
        _accept_turn=lambda text: None, _new_operation=operation,
        _disarm=lambda: None, _transition=lambda state: None,
        is_current=lambda value: value is current[0], _speak_answer=lambda *a, **kw: None,
        _begin_listening=lambda **kw: None, _arm=lambda duration: None,
        FOLLOWUP_WINDOW_SECONDS=10)
    def speak(text, owner):
        done = asyncio.Event(); done.status = 'success'; done.set(); return done
    rt.callbacks = SimpleNamespace(speak=speak)
    source = DictationTools(service, lambda: rt.voice_member_id, lambda: 'seed', device_id='tv')
    rt.conversation_paths.dictation_service = service
    rt.conversation_paths.dictation = source
    adapter = ClassicDictation(rt)
    assert await adapter.handle('帮我默写第二课')
    assert adapter.tools.coordinator.closed is False
    first_session = adapter.preparation.trusted['session_id']
    assert await adapter.handle('一年级下册语文')
    assert adapter.preparation.trusted['session_id'] == first_session
    assert len(calls) == 2
    assert all(call['model'] == 'kimi-k3' and call['temperature'] == .2 for call in calls)
    assert {tool['function']['name'] for tool in calls[0]['tools']} == {
        'get_dictation_context', 'prepare_dictation_task'}
    assert not await adapter.handle('退出')
    rt.voice_member_id = None
    assert not await adapter.handle('帮我默写第二课')
    await adapter.close()
