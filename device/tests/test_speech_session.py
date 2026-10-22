import asyncio
import numpy as np
from test_audio_stack import playback_engine, HeldPlayer

async def test_live_session_overlaps_and_waits_for_physical_drain():
    calls = []
    engine = playback_engine(HeldPlayer(), lambda text, speed: calls.append(text) or np.ones(8, dtype=np.float32))
    session = engine.start_session(owner=7)
    await session.feed('第一句话。')
    await engine._player.started.wait()
    await session.feed('第二句话。')
    for _ in range(100):
        if len(calls) == 2:
            break
        await asyncio.sleep(.001)
    assert len(calls) == 2
    session.finish_input()
    assert not session.done.is_set()
    engine._player.release.set()
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'success'
    await engine.close()

async def test_finite_requests_have_capacity():
    engine = playback_engine(HeldPlayer())
    completions = [engine.speak('hello') for _ in range(40)]
    assert any(done.status == 'failed' for done in completions)
    await engine.close()

async def test_native_cancel_does_not_overlap_successor_inference():
    import threading
    entered, release = threading.Event(), threading.Event()
    active = 0
    maximum = 0
    def generate(text, speed):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if text == 'old':
            entered.set()
            release.wait(2)
        active -= 1
        return np.ones(8, dtype=np.float32)
    player = HeldPlayer()
    player.release.set()
    engine = playback_engine(player, generate)
    old = engine.speak('old', owner=1)
    await asyncio.to_thread(entered.wait, 1)
    engine.cancel_owner(1)
    successor = engine.speak('new', owner=2)
    await asyncio.sleep(.02)
    assert not successor.is_set()
    assert not player.played
    release.set()
    await asyncio.wait_for(successor.wait(), 1)
    assert maximum == 1
    assert old.status == 'cancelled'
    assert len(player.played) == 1
    await engine.close()

async def test_input_backpressure_is_bounded_and_cancel_wakes_feeder():
    engine = playback_engine(HeldPlayer())
    # Hold coordinator on an earlier request so input cannot be consumed.
    engine.speak('hold', owner=1)
    await engine._player.started.wait()
    session = engine.start_session(owner=2)
    feed = asyncio.create_task(session.feed('字' * 1000))
    await asyncio.sleep(.01)
    assert session.pending_chars == 256
    assert not feed.done()
    engine.cancel_owner(2)
    import pytest
    with pytest.raises(RuntimeError, match='closed'):
        await feed
    assert session.done.status == 'cancelled'
    await engine.close()

async def test_production_stream_is_single_and_chunks_are_bounded():
    class StreamPlayer(HeldPlayer):
        def __init__(self):
            super().__init__()
            self.streams = 0
        async def play_stream(self, chunks):
            self.streams += 1
            async for pcm in chunks:
                self.played.append(pcm)
                self.started.set()
            await self.release.wait()
    player = StreamPlayer()
    engine = playback_engine(player, lambda text, speed: np.ones(22050 * 5, dtype=np.float32))
    session = engine.start_session()
    await session.feed('one')
    await player.started.wait()
    await session.feed('two')
    session.finish_input()
    await asyncio.sleep(.02)
    assert not session.done.is_set()
    assert engine.occupied
    player.release.set()
    await asyncio.wait_for(session.done.wait(), 1)
    assert player.streams == 1
    assert max(map(len, player.played)) <= engine.sample_rate * 4
    assert sum(map(len, player.played)) == engine.sample_rate * 5 * 2 * 2
    await engine.close()

async def test_empty_session_does_not_claim_output_and_close_wakes_input():
    engine = playback_engine(HeldPlayer())
    first = engine.start_session()
    first.finish_input()
    await asyncio.wait_for(first.done.wait(), 1)
    assert first.done.status == 'success'
    assert not engine.occupied
    assert not engine._player.played
    waiting = engine.start_session(owner=4)
    await asyncio.sleep(.01)
    await asyncio.wait_for(engine.close(), 1)
    assert waiting.done.status == 'cancelled'

async def test_synthesis_failure_is_terminal_without_output():
    def broken(text, speed):
        raise RuntimeError('synthesis broke')
    engine = playback_engine(HeldPlayer(), broken)
    session = engine.start_session()
    await session.feed('hello')
    session.finish_input()
    await asyncio.wait_for(session.done.wait(), 1)
    assert session.done.status == 'failed'
    assert not engine._player.played
    await engine.close()

async def test_failed_synthesis_wakes_blocked_input_producer():
    import threading
    import pytest
    release = threading.Event()
    def broken(text, speed):
        release.wait(1)
        raise RuntimeError('failed native synthesis')
    engine = playback_engine(HeldPlayer(), broken)
    session = engine.start_session()
    producer = asyncio.create_task(session.feed('字' * 2000))
    await asyncio.sleep(.02)
    assert not producer.done()
    assert session.pending_chars <= 256
    release.set()
    with pytest.raises(RuntimeError, match='closed'):
        await asyncio.wait_for(producer, 1)
    assert session.done.status == 'failed'
    await engine.close()
