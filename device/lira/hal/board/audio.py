"""Continuous, bounded callback PCM output for the board sound device."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
import threading

import sounddevice as sd

from lira.hal.base import AudioIO, HalError
from lira.audio.voice_metrics import current_trace, current_answer, mark

__all__ = ['SoundDevicePlayer']


@dataclass
class _Output:
    trace: object = None
    answer: bool = True
    stopped: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    drained: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stream: object | None = None
    queue: deque = field(default_factory=deque)
    submitted: int = 0
    consumed: int = 0
    closed: bool = False
    first: bool = True
    error: Exception | None = None
    producer: asyncio.Task | None = None
    worker_started: bool = False
    starving: bool = False


class SoundDevicePlayer(AudioIO):
    """One RawOutputStream per answer, with at most two seconds queued PCM."""

    def __init__(self, device: int | str | None = None, samplerate: int = 22050) -> None:
        self.output_frames = 0
        self.device = device
        self.samplerate = int(samplerate)
        self._play_lock = asyncio.Lock()
        self._output: _Output | None = None

    async def open(self) -> None:
        try:
            sd.check_output_settings(device=self.device, samplerate=self.samplerate,
                                     channels=1, dtype='int16')
        except (OSError, sd.PortAudioError) as exc:
            raise HalError(f'音频输出设备不可用: {exc}') from exc

    async def close(self) -> None:
        await self.stop()

    async def read_chunk(self, size: int) -> bytes:
        raise NotImplementedError('SoundDevicePlayer 仅播放，不采集')

    async def play(self, data: bytes) -> None:
        async def finite():
            capacity = max(2, self.samplerate * 4)
            for position in range(0, len(data), capacity):
                yield data[position:position + capacity]
        await self.play_stream(finite())

    async def play_stream(self, chunks: AsyncIterable[bytes]) -> None:
        async with self._play_lock:
            output = self._output = _Output(trace=current_trace.get(), answer=current_answer.get())
            capacity = max(2, self.samplerate * 4)

            async def produce():
                iterator = aiter(chunks)
                try:
                    async for data in iterator:
                        if len(data) % 2:
                            raise HalError('PCM 必须包含完整 int16 帧')
                        if len(data) > capacity:
                            raise HalError('连续 PCM 输入块不能超过两秒')
                        pcm = memoryview(data)
                        position = 0
                        while position < len(pcm):
                            if output.stopped.is_set():
                                return
                            available = capacity - (output.submitted - output.consumed)
                            if available <= 0:
                                # Async backpressure never blocks the callback or stop.
                                await asyncio.sleep(.002)
                                continue
                            size = min(available, len(pcm) - position)
                            output.submitted += size
                            output.queue.append(pcm[position:position + size])
                            position += size
                finally:
                    output.closed = True
                    close = getattr(iterator, 'aclose', None)
                    if close is not None:
                        await close()

            output.producer = asyncio.create_task(produce())
            task = None
            try:
                # Preload finite inputs before synchronous test/device callbacks start.
                await asyncio.sleep(0)
                # A slow source must not open hardware and manufacture startup
                # starvation. Start after 100ms prepared audio, or finite EOF.
                prebuffer = max(2, int(self.samplerate * .1) * 2)
                first_buffered_at = None
                while output.submitted < prebuffer and not output.closed:
                    if output.stopped.is_set():
                        return
                    if output.submitted:
                        now = asyncio.get_running_loop().time()
                        if first_buffered_at is None:
                            first_buffered_at = now
                        elif now - first_buffered_at >= .1:
                            break
                    await asyncio.sleep(.002)
                if output.stopped.is_set():
                    return
                if output.producer.done():
                    await output.producer
                if output.closed and not output.queue:
                    return
                output.worker_started = True
                task = asyncio.create_task(asyncio.to_thread(self._play_blocking, output))
                done, _ = await asyncio.wait((task, output.producer), return_when=asyncio.FIRST_COMPLETED)
                if output.producer in done and not output.stopped.is_set():
                    await output.producer
                await asyncio.shield(task)
                if not output.stopped.is_set():
                    await output.producer
            except BaseException:
                output.producer.cancel()
                if task is not None:
                    await self._stop_output(output)
                    await task
                raise
            finally:
                output.producer.cancel()
                await asyncio.gather(output.producer, return_exceptions=True)
                if self._output is output:
                    self._output = None

    async def stop(self) -> None:
        output = self._output
        if output is not None:
            await self._stop_output(output)

    async def _stop_output(self, output: _Output) -> None:
        output.stopped.set()
        if output.producer is not None:
            output.producer.cancel()
        try:
            await asyncio.to_thread(self._abort_blocking, output)
        finally:
            # No worker exists yet during the preload scheduling turn.
            if output.worker_started:
                await asyncio.to_thread(output.finished.wait)

    @staticmethod
    def _abort_blocking(output: _Output) -> None:
        with output.lock:
            if output.stream is not None and not output.finished.is_set():
                try:
                    output.stream.abort()
                    output.drained.set()
                except (OSError, sd.PortAudioError) as exc:
                    output.error = HalError(f'停止音频失败: {exc}')
                    output.drained.set()  # Worker closes output; this is NOT success.
                    raise HalError(f'停止音频失败: {exc}') from exc

    def _play_blocking(self, output: _Output) -> None:
        pending = memoryview(b'')
        def callback(outdata, frames, time_info, status):
            nonlocal pending
            size = frames * 2
            outdata[:] = b'\0' * size
            if output.stopped.is_set():
                raise sd.CallbackAbort
            if getattr(status, 'output_underflow', False):
                mark(output.trace, 'buffer', device_underflow=1)
                output.error = HalError('音频输出设备 underflow')
                raise sd.CallbackAbort
            position = 0
            while position < size:
                if not pending:
                    try:
                        pending = output.queue.popleft()
                    except IndexError:
                        break
                count = min(size - position, len(pending))
                outdata[position:position + count] = pending[:count]
                pending = pending[count:]
                position += count
            output.consumed += position
            self.output_frames += position // 2
            mark(output.trace, 'buffer', buffer_frames=(output.submitted-output.consumed)//2)
            if position and output.first:
                output.first = False
                mark(output.trace, 'output_consumed', frames=position // 2, answer=int(output.answer))
            if output.closed and not pending and not output.queue:
                raise sd.CallbackStop
            if position < size:
                mark(output.trace, 'buffer', starvation_count=int(not output.starving),
                     starvation_seconds=(size - position) / (2 * self.samplerate))
                output.starving = True
            else:
                output.starving = False

        stream = None
        try:
            stream = sd.RawOutputStream(device=self.device, samplerate=self.samplerate,
                                        channels=1, dtype='int16', callback=callback,
                                        finished_callback=output.drained.set)
            with output.lock:
                output.stream = stream
                if output.stopped.is_set():
                    return
                stream.start()
            # PortAudio finished_callback follows CallbackStop's physical drain,
            # not the callback that merely copied the last PCM into a device buffer.
            while not output.drained.wait(.05):
                if getattr(stream, 'active', True) is False:
                    raise HalError('音频输出设备意外停止')
            if output.error is not None:
                raise output.error
            if not output.stopped.is_set() and (not output.closed or output.consumed != output.submitted):
                raise HalError('音频输出未排空即停止')
            mark(output.trace, 'cancel_confirmed' if output.stopped.is_set() else 'output_drained')
        except (OSError, sd.PortAudioError) as exc:
            if not output.stopped.is_set():
                raise HalError(f'播放失败: {exc}') from exc
        finally:
            with output.lock:
                try:
                    if stream is not None:
                        stream.close()
                finally:
                    output.stream = None
                    output.finished.set()
