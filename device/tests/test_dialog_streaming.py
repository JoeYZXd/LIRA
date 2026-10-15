import asyncio
from pathlib import Path
import pytest
from lira.dialog import DialogEngine, Router, State
from lira.hal.mock.audio import MockAudioIO
from test_audio_stack import playback_engine
from test_state_machine import Recorder


class SessionRecorder(Recorder):
    def __init__(self, tts):
        super().__init__()
        self.tts = tts
        self.sessions = []
        self.answer_flags = []
        self.remembered = []

    def start_answer(self, *, owner):
        from lira.audio.voice_metrics import current_answer
        self.answer_flags.append(current_answer.get())
        session = self.tts.start_session(owner=owner)
        self.sessions.append(session)
        return session

    async def feed_answer(self, session, text, *, owner):
        await session.feed(text)

    def cancel_audio(self, owner):
        self.tts.cancel_owner(owner)
        asyncio.create_task(self.tts.stop(owner=owner))

    def remember_turn(self, user, answer):
        self.remembered.append((user, answer))


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(poll(), 2)


@pytest.mark.parametrize('failure', [False, True])
async def test_answer_streams_while_model_waits_and_commits_only_after_drain(failure):
    player = MockAudioIO(Path('unused.wav'), hold_playback=True)
    tts = playback_engine(player)
    cb = SessionRecorder(tts)
    generated = asyncio.Event()
    release = asyncio.Event()
    async def remote(text, *, on_reply_chunk, **kwargs):
        await on_reply_chunk('第一段已经能够播报，需要先让用户听见。')
        generated.set()
        await release.wait()
        if failure:
            raise RuntimeError('model failed after partial output')
        await on_reply_chunk('第二段也要完整播完。')
        return '第一段已经能够播报，需要先让用户听见。第二段也要完整播完。'
    engine = DialogEngine(Router(remote_llm=remote, remote_available=lambda: True), cb)
    await engine.on_wake()
    task = asyncio.create_task(engine.on_asr_text('介绍一下这个世界'))
    try:
        await asyncio.wait_for(generated.wait(), 2)
        await until(lambda: bool(player.played))
        assert not task.done() and len(cb.sessions) == 1
        assert engine.state is State.PROCESSING and cb.remembered == []
        assert engine._history.snapshot().pairs == ()
        release.set()
        await task
        if failure:
            await until(lambda: cb.sessions[0].done.is_set())
            assert cb.remembered == [] and engine._history.snapshot().pairs == ()
        else:
            assert engine.state is State.SPEAKING and cb.remembered == []
            player.playback_released.set()
            await until(lambda: engine.state is State.FOLLOWUP)
            assert len(cb.remembered) == 1 and len(engine._history.snapshot().pairs) == 1
        assert len(cb.sessions) == 1 and cb.answer_flags == [True]
    finally:
        release.set()
        engine.close()
        await tts.close()


async def test_cancel_while_waiting_for_model_cannot_commit_partial_answer():
    player = MockAudioIO(Path('unused.wav'))
    tts = playback_engine(player)
    cb = SessionRecorder(tts)
    entered = asyncio.Event()
    async def remote(text, *, on_reply_chunk, **kwargs):
        await on_reply_chunk('这一段在模型返回前就可以开始朗读。')
        entered.set()
        await asyncio.Event().wait()
    engine = DialogEngine(Router(remote_llm=remote, remote_available=lambda: True), cb)
    await engine.on_wake()
    task = asyncio.create_task(engine.on_asr_text('介绍一下这个世界'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await until(lambda: bool(player.played))
        await engine.on_wake()
        await task
        await until(lambda: cb.sessions[0].done.is_set())
        assert cb.sessions[0].done.status == 'cancelled'
        assert cb.remembered == [] and engine._history.snapshot().pairs == ()
    finally:
        engine.close()
        await tts.close()

async def test_device_answer_seam_preserves_settings_observation_and_gates():
    from types import SimpleNamespace
    from lira.main import DeviceCallbacks
    from collections import deque
    from lira.audio.voice_metrics import current_answer
    class FakeTts:
        accepts_text_deltas = True
        def start_session(self, **kwargs):
            self.settings = kwargs
            self.answer = current_answer.get()
            return SimpleNamespace(feed=self.feed, cancel=lambda: None)
        async def feed(self, text):
            self.text = text
    tts = FakeTts()
    rt = SimpleNamespace(tts=tts, settings=SimpleNamespace(tts_speed=1.2, volume=.4),
                         engine=SimpleNamespace(dnd=False, is_current=lambda owner: owner == 7),
                         privacy=SimpleNamespace(is_on=False), recent_speaks=deque())
    cb = DeviceCallbacks(rt)
    session = cb.start_answer(owner=7)
    await cb.feed_answer(session, '测试播报', owner=7)
    assert tts.settings == {'speed':1.2, 'volume':.4, 'owner':7}
    assert list(rt.recent_speaks) == ['测试播报'] and tts.text == '测试播报'
    assert cb.accepts_text_deltas
    assert cb.start_answer(owner=8) is None
    rt.engine.dnd = True
    assert cb.start_answer(owner=7) is None
    rt.engine.dnd = False
    rt.privacy.is_on = True
    assert cb.start_answer(owner=7) is None


async def test_output_error_does_not_commit_complete_answer():
    class BrokenAudio(MockAudioIO):
        async def play_stream(self, chunks):
            async for _ in chunks:
                raise RuntimeError('output failed')
    tts = playback_engine(BrokenAudio(Path('unused.wav')))
    cb = SessionRecorder(tts)
    async def remote(text, *, on_reply_chunk, **kwargs):
        await on_reply_chunk('回答已经生成，但是设备输出发生故障。')
        return '回答已经生成，但是设备输出发生故障。'
    engine = DialogEngine(Router(remote_llm=remote, remote_available=lambda: True), cb)
    await engine.on_wake()
    try:
        await engine.on_asr_text('介绍一下这个世界')
        await until(lambda: cb.sessions[0].done.is_set())
        await asyncio.sleep(0)
        assert cb.sessions[0].done.status == 'failed'
        assert cb.remembered == [] and engine._history.snapshot().pairs == ()
        assert engine.state is State.LISTENING
    finally:
        engine.close()
        await tts.close()

