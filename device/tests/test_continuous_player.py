import asyncio
import pytest
from test_hal_mock import board_player_module

async def test_stream_uses_one_output_and_waits_for_final_drain(monkeypatch):
    board = board_player_module(monkeypatch)
    streams = []
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
    assert streams[0].consume() == b'\1\0\2\0'
    assert not task.done()
    assert streams[0].consume() == b'\0' * 4
    more.set()
    await asyncio.sleep(.01)
    assert streams[0].consume() == b'\3\0\4\0'
    assert not task.done()
    streams[0].finished()
    await asyncio.wait_for(task, 1)
    assert len(streams) == 1

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
