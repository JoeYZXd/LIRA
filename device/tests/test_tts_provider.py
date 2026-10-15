import pytest
from lira.audio.tts_provider import OnlineTtsConfig

def test_secrets_and_default_off():
    assert not OnlineTtsConfig().enabled
    assert 'secret' not in repr(OnlineTtsConfig(api_key='secret'))

import asyncio
import numpy as np
from test_audio_stack import playback_engine
from lira.hal.mock.audio import MockAudioIO

async def test_online_before_sound_fallback_once_and_finite_local(tmp_path):
    player = MockAudioIO(tmp_path/'unused')
    calls = []
    engine = playback_engine(player, lambda text, speed: calls.append(text) or np.ones(8, dtype=np.float32))
    class Failing:
        async def stream(self, text, **kwargs):
            async for delta in text:
                raise RuntimeError('failed')
                yield b''
    engine._online = Failing()
    engine._online_allowed = lambda: True
    session = engine.start_session(owner=1)
    await session.feed('正文。')
    session.finish_input()
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'success'
    assert calls == ['正文。']
    assert not engine.accepts_text_deltas
    await engine.close()

async def test_output_error_wakes_open_session_and_successor():
    class Player:
        def __init__(self):
            self.fail = True
        async def play_stream(self, source):
            async for pcm in source:
                await asyncio.sleep(.01)
                if self.fail:
                    self.fail = False
                    raise RuntimeError('device failed')
        async def stop(self): pass
        async def close(self): pass
    engine = playback_engine(Player())
    session = engine.start_session(owner=1)
    await session.feed('正文。')
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'failed'
    successor = engine.speak('下一轮', owner=2)
    await asyncio.wait_for(successor.wait(), 1)
    assert successor.status == 'success'
    await engine.close()

async def test_online_after_sound_failure_never_replays(tmp_path):
    player = MockAudioIO(tmp_path/'unused')
    calls = []
    engine = playback_engine(player, lambda text, speed: calls.append(text) or np.ones(8, dtype=np.float32))
    class Failing:
        async def stream(self, text, **kwargs):
            async for delta in text:
                yield b'\x01\x00'
                raise RuntimeError('failed')
    engine._online = Failing()
    engine._online_allowed = lambda: True
    session = engine.start_session(owner=1)
    await session.feed('正文。')
    session.finish_input()
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'failed'
    assert not calls
    assert len(player.played) == 1
    assert not engine.accepts_text_deltas
    await engine.close()

async def test_online_privacy_closes_stalled_session(tmp_path):
    from lira.privacy import PrivacyState
    player = MockAudioIO(tmp_path/'unused')
    engine = playback_engine(player)
    privacy = PrivacyState()
    entered, closed = asyncio.Event(), asyncio.Event()
    class Stalled:
        async def stream(self, text, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield b''
            finally:
                closed.set()
    engine._online = Stalled()
    engine._online_allowed = lambda: not privacy.is_on
    privacy.subscribe(engine.apply_privacy)
    session = engine.start_session(owner=1)
    await session.feed('正文。')
    await entered.wait()
    await asyncio.wait_for(privacy.set_enabled(True, source='test'), 1)
    await asyncio.wait_for(session.done.wait(), 1)
    assert closed.is_set()
    assert session.done.status == 'cancelled'
    assert not player.played
    assert session.pending_chars == 0
    await engine.close()

async def test_silent_online_success_falls_back(tmp_path):
    player = MockAudioIO(tmp_path/'unused')
    calls = []
    engine = playback_engine(player, lambda text, speed: calls.append(text) or np.ones(8, dtype=np.float32))
    class Silent:
        async def stream(self, text, **kwargs):
            async for delta in text:
                pass
            if False: yield b''
    engine._online = Silent()
    engine._online_allowed = lambda: True
    session = engine.start_session(owner=1)
    await session.feed('正文。')
    session.finish_input()
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'success'
    assert calls == ['正文。']
    await engine.close()

async def test_disabled_missing_credentials_and_finite_cues_never_call_remote(tmp_path):
    player = MockAudioIO(tmp_path/'unused')
    engine = playback_engine(player)
    for config in (OnlineTtsConfig(), OnlineTtsConfig(enabled=True)):
        engine.configure_online(config, allowed=lambda: True)
        assert not engine.accepts_text_deltas
        session = engine.start_session()
        await session.feed('本地。')
        session.finish_input()
        await asyncio.wait_for(session.done.wait(), 1)
        assert session.done.status == 'success'
    class Forbidden:
        def stream(self, *args, **kwargs):
            raise AssertionError('finite cue must stay local')
    engine._online = Forbidden()
    engine._online_allowed = lambda: True
    done = engine.speak('提示。')
    await asyncio.wait_for(done.wait(), 1)
    assert done.status == 'success'
    await engine.close()

async def test_replay_budget_failure_never_restarts_full_text(tmp_path):
    player = MockAudioIO(tmp_path/'unused')
    calls = []
    engine = playback_engine(player, lambda text, speed: calls.append(text) or np.ones(8, dtype=np.float32))
    class Failing:
        async def stream(self, text, **kwargs):
            async for delta in text:
                pass
            raise RuntimeError('network')
            yield b''
    engine._online = Failing()
    engine._online_allowed = lambda: True
    session = engine.start_session()
    await session.feed('字'*300)
    session.finish_input()
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'failed'
    assert not calls and not player.played
    await engine.close()

async def test_fallback_uses_safe_local_segmentation_for_tiny_deltas(tmp_path):
    player = MockAudioIO(tmp_path/'unused')
    calls = []
    engine = playback_engine(player, lambda text, speed: calls.append(text) or np.ones(8, dtype=np.float32))
    failed = asyncio.Event()
    class Failing:
        async def stream(self, text, **kwargs):
            async for delta in text:
                failed.set()
                raise RuntimeError('network')
                yield b''
    engine._online = Failing()
    engine._online_allowed = lambda: True
    session = engine.start_session()
    await session.feed('T')
    await failed.wait()
    for char in 'he number is 3.14 and English words remain intact.':
        await session.feed(char)
    session.finish_input()
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'success'
    assert '3.14' in ' '.join(calls)
    assert all(len(chunk) > 1 for chunk in calls)
    assert 'English' in ' '.join(calls)
    await engine.close()
