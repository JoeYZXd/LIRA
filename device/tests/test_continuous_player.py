import asyncio
import pytest
from test_hal_mock import board_player_module

async def test_stream_uses_one_output_and_waits_for_final_drain(monkeypatch):
    board = board_player_module(monkeypatch)
    streams = []
    marks = []
    monkeypatch.setattr(board, 'mark', lambda trace, event, **fields: marks.append((event, fields)))
    class Stream:
        def __init__(self, **kw):
            self.callback = kw['callback']; self.finished = kw['finished_callback']
            streams.append(self)
        def start(self): pass
        def close(self): pass
        def abort(self): self.finished()
        def consume(self):
            data = bytearray(4)
            try: self.callback(data, 2, None, None)
            except board.sd.CallbackStop: pass
            return bytes(data)
    monkeypatch.setattr(board.sd, 'RawOutputStream', Stream)
    more = asyncio.Event()
    async def source():
        yield b'\1\0\2\0'
        await more.wait()
        yield b'\3\0\4\0'
    player = board.SoundDevicePlayer(samplerate=4)
    task = asyncio.create_task(player.play_stream(source()))
    for _ in range(100):
        if streams: break
        await asyncio.sleep(.001)
    assert len(streams) == 1
    assert player.output_frames == 0
    assert streams[0].consume() == b'\1\0\2\0'
    assert player.output_frames == 2
    assert not task.done()
    assert streams[0].consume() == b'\0' * 4
    more.set()
    await asyncio.sleep(.01)
    assert streams[0].consume() == b'\3\0\4\0'
    assert not task.done()
    streams[0].finished()
    await asyncio.wait_for(task, 1)
    assert len(streams) == 1
    assert player.output_frames == 4
    occupancy = [fields['buffer_frames'] for event, fields in marks if 'buffer_frames' in fields]
    assert occupancy and all(0 <= value <= 8 for value in occupancy)
    assert occupancy[-1] == 0

async def test_pcm_rejects_partial_frame(monkeypatch):
    board = board_player_module(monkeypatch)
    async def source(): yield b'\1'
    with pytest.raises(board.HalError):
        await board.SoundDevicePlayer().play_stream(source())

async def test_backpressure_is_bounded_and_stop_cancels_waiting_source(monkeypatch):
    board = board_player_module(monkeypatch)
    streams = []
    class Stream:
        def __init__(self, **kw):
            self.finished = kw['finished_callback']; streams.append(self)
        def start(self): pass
        def close(self): pass
        def abort(self): self.finished()
    monkeypatch.setattr(board.sd, 'RawOutputStream', Stream)
    produced = []
    closed = asyncio.Event()
    async def source():
        try:
            for number in range(20):
                produced.append(number)
                yield b'\1\0' * 4
        finally:
            closed.set()
    player = board.SoundDevicePlayer(samplerate=4)
    task = asyncio.create_task(player.play_stream(source()))
    while not streams:
        await asyncio.sleep(.001)
    await asyncio.sleep(.01)
    assert len(produced) == 3  # 2 seconds queued plus one producer chunk
    assert player._output.submitted - player._output.consumed == 16
    await asyncio.wait_for(player.stop(), 1)
    await asyncio.wait_for(task, 1)
    assert closed.is_set()


async def test_underflow_is_failure_and_not_successful_drain(monkeypatch):
    from types import SimpleNamespace
    board = board_player_module(monkeypatch)
    class Stream:
        def __init__(self, **kw):
            self.callback = kw['callback']; self.finished = kw['finished_callback']
        def start(self):
            try:
                self.callback(bytearray(4), 2, None, SimpleNamespace(output_underflow=True))
            except board.sd.CallbackAbort:
                self.finished()
        def close(self): pass
        def abort(self): self.finished()
    monkeypatch.setattr(board.sd, 'RawOutputStream', Stream)
    with pytest.raises(board.HalError, match='underflow'):
        await board.SoundDevicePlayer().play(b'\1\0')


async def test_mock_stream_stop_unblocks_source():
    from pathlib import Path
    from lira.hal.mock.audio import MockAudioIO
    player = MockAudioIO(Path('/unused'))
    entered = asyncio.Event()
    async def source():
        yield b'\1\0'
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(player.play_stream(source()))
    await entered.wait()
    await asyncio.wait_for(player.stop(), 1)
    await task
    assert player.played == [b'\1\0']

async def test_finished_before_consumption_fails(monkeypatch):
    board = board_player_module(monkeypatch)
    class Stream:
        def __init__(self, **kw): self.finished = kw['finished_callback']
        def start(self): self.finished()
        def close(self): pass
        def abort(self): self.finished()
    monkeypatch.setattr(board.sd, 'RawOutputStream', Stream)
    with pytest.raises(board.HalError, match='未排空'):
        await board.SoundDevicePlayer().play(b'\1\0')


async def test_stalled_first_input_does_not_open_device(monkeypatch):
    board = board_player_module(monkeypatch)
    opened = []
    monkeypatch.setattr(board.sd, 'RawOutputStream', lambda **kw: opened.append(kw))
    entered = asyncio.Event()
    async def source():
        entered.set()
        await asyncio.Event().wait()
        yield b'\1\0'
    player = board.SoundDevicePlayer()
    task = asyncio.create_task(player.play_stream(source()))
    await entered.wait()
    await asyncio.sleep(.01)
    assert not opened
    await player.stop()
    await asyncio.wait_for(task, 1)


async def test_abort_failure_propagates_after_output_close(monkeypatch):
    board = board_player_module(monkeypatch)
    streams = []
    class Stream:
        def __init__(self, **kw):
            streams.append(self); self.closed = False
        def start(self): pass
        def close(self): self.closed = True
        def abort(self): raise OSError('unplugged')
    monkeypatch.setattr(board.sd, 'RawOutputStream', Stream)
    player = board.SoundDevicePlayer()
    task = asyncio.create_task(player.play(b'\1\0'))
    while not streams:
        await asyncio.sleep(.001)
    with pytest.raises(board.HalError, match='停止音频失败'):
        await player.stop()
    assert streams[0].closed
    with pytest.raises(board.HalError):
        await task

async def test_partial_prebuffer_starts_despite_stalled_upstream(monkeypatch):
    board = board_player_module(monkeypatch)
    opened = asyncio.Event()
    loop = asyncio.get_running_loop()
    class Stream:
        def __init__(self, **kw): self.finished = kw['finished_callback']
        def start(self): loop.call_soon_threadsafe(opened.set)
        def close(self): pass
        def abort(self): self.finished()
    monkeypatch.setattr(board.sd, 'RawOutputStream', Stream)
    async def source():
        yield b'\1\0'
        await asyncio.Event().wait()
    player = board.SoundDevicePlayer()
    task = asyncio.create_task(player.play_stream(source()))
    await asyncio.wait_for(opened.wait(), .8)
    await player.stop()
    await asyncio.wait_for(task, 1)


def install_metrics_stream(monkeypatch, board):
    """Expose callbacks without making starvation duration depend on wall time."""
    loop = asyncio.get_running_loop()
    opened = asyncio.Event()
    streams = []

    class Stream:
        def __init__(self, **kw):
            self.callback = kw['callback']
            self.finished = kw['finished_callback']
            streams.append(self)

        def start(self):
            loop.call_soon_threadsafe(opened.set)

        def close(self):
            pass

        def abort(self):
            self.finished()

        def consume(self, frames):
            data = bytearray(frames * 2)
            stopped = False
            try:
                self.callback(data, frames, None, None)
            except board.sd.CallbackStop:
                stopped = True
            return bytes(data), stopped

    monkeypatch.setattr(board.sd, 'RawOutputStream', Stream)
    return opened, streams


@pytest.mark.parametrize('enabled', [False, True])
async def test_starvation_counts_episodes_and_missing_frames(monkeypatch, enabled):
    from lira.audio.voice_metrics import VoiceMetrics, current_trace

    board = board_player_module(monkeypatch)
    opened, streams = install_metrics_stream(monkeypatch, board)
    metrics = VoiceMetrics(enabled=True) if enabled else VoiceMetrics()
    assert metrics.enabled is enabled
    resume = asyncio.Event()
    recovered = asyncio.Event()
    finish = asyncio.Event()
    eof = asyncio.Event()

    async def source():
        yield b'\1\0' * 2
        await resume.wait()
        yield b'\0' * 4  # Real silent PCM is still producer-supplied audio.
        recovered.set()
        await finish.wait()
        eof.set()

    player = board.SoundDevicePlayer(samplerate=8)
    token = current_trace.set(metrics.trace(utterance_id=42))
    task = asyncio.create_task(player.play_stream(source()))
    current_trace.reset(token)
    try:
        await asyncio.wait_for(opened.wait(), 1)
        stream = streams[0]
        # Two content frames and one missing frame begin the first episode.
        assert stream.consume(3) == (b'\1\0' * 2 + b'\0' * 2, False)
        assert stream.consume(2) == (b'\0' * 4, False)
        assert stream.consume(1) == (b'\0' * 2, False)
        resume.set()
        await asyncio.wait_for(recovered.wait(), 1)
        assert stream.consume(2) == (b'\0' * 4, False)
        # A full callback of supplied zero PCM ends the previous episode.
        assert stream.consume(2) == (b'\0' * 4, False)
        rows = [row for row in metrics.snapshot() if 'starvation_count' in row]
        if enabled:
            assert [row['starvation_count'] for row in rows] == [1, 0, 0, 1]
            assert [row['starvation_seconds'] for row in rows] == pytest.approx(
                [1 / 8, 2 / 8, 1 / 8, 2 / 8])
            assert sum(row['starvation_count'] for row in rows) == 2
            assert sum(row['starvation_seconds'] for row in rows) == pytest.approx(6 / 8)
            assert all(row['utterance_id'] == 42 for row in rows)
        else:
            assert metrics.snapshot() == []
        assert player.output_frames == 4
        finish.set()
        await asyncio.wait_for(eof.wait(), 1)
        assert stream.consume(2) == (b'\0' * 4, True)
        assert [row for row in metrics.snapshot() if 'starvation_count' in row] == rows
        stream.finished()
        await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            await player.stop()
            await asyncio.wait_for(task, 1)


async def test_finite_silent_pcm_and_eof_padding_are_not_starvation(monkeypatch):
    from lira.audio.voice_metrics import VoiceMetrics, current_trace

    board = board_player_module(monkeypatch)
    opened, streams = install_metrics_stream(monkeypatch, board)
    metrics = VoiceMetrics(enabled=True)
    player = board.SoundDevicePlayer(samplerate=8)
    token = current_trace.set(metrics.trace(utterance_id=43))
    task = asyncio.create_task(player.play(b'\0' * 6))
    current_trace.reset(token)
    try:
        await asyncio.wait_for(opened.wait(), 1)
        stream = streams[0]
        assert stream.consume(2) == (b'\0' * 4, False)
        # The final frame is content; the remaining three are finite EOF padding.
        assert stream.consume(4) == (b'\0' * 8, True)
        assert player.output_frames == 3
        stream.finished()
        await asyncio.wait_for(task, 1)
        rows = metrics.snapshot()
        assert any(row['event'] == 'output_consumed' for row in rows)
        assert any(row['event'] == 'output_drained' for row in rows)
        assert not any('starvation_count' in row or 'starvation_seconds' in row for row in rows)
    finally:
        if not task.done():
            await player.stop()
            await asyncio.wait_for(task, 1)
