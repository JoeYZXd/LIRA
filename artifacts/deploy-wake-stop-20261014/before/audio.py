"""板上播放器 HAL（U10）：sounddevice 输出（ES8388 → 3.5mm → 有源音箱）。

TTS 以 matcha 模型采样率合成后送 `play()`；输出设备默认走 PipeWire 默认
sink（板载 ES8388 模拟口），软音量在播报层实现（R21"大声点/小声点"）。
采集侧直接复用 `lira.audio.mic.SoundDeviceMic`（板上/实机共用，U2 交付）。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

import sounddevice as sd

from lira.hal.base import AudioIO, HalError

__all__ = ["SoundDevicePlayer"]


@dataclass
class _Output:
    stopped: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stream: object | None = None


class SoundDevicePlayer(AudioIO):
    """`AudioIO` 播放侧实现：每次 play 开一次性 OutputStream（块级生命周期）。"""

    def __init__(self, device: int | str | None = None, samplerate: int = 22050) -> None:
        self.device = device
        self.samplerate = int(samplerate)
        self._play_lock = asyncio.Lock()
        self._output: _Output | None = None

    async def open(self) -> None:
        """探测输出设备（默认 sink 存在即通过）。"""
        try:
            sd.check_output_settings(
                device=self.device, samplerate=self.samplerate,
                channels=1, dtype="int16",
            )
        except (OSError, sd.PortAudioError) as exc:
            raise HalError(f"音频输出设备不可用: {exc}") from exc

    async def close(self) -> None:
        await self.stop()

    async def read_chunk(self, size: int) -> bytes:
        raise NotImplementedError("SoundDevicePlayer 仅播放，不采集")

    async def play(self, data: bytes) -> None:
        """播放 int16 单声道 PCM（阻塞写在线程池，不卡事件循环）。"""
        async with self._play_lock:
            output = self._output = _Output()
            task = asyncio.create_task(asyncio.to_thread(self._play_blocking, data, output))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await self.stop()
                await task
                raise
            finally:
                if self._output is output:
                    self._output = None

    async def stop(self) -> None:
        # Snapshot identity: a delayed stop can never abort the next stream.
        output = self._output
        if output is None:
            return
        output.stopped.set()
        await asyncio.to_thread(self._abort_blocking, output)
        await asyncio.to_thread(output.finished.wait)

    @staticmethod
    def _abort_blocking(output: _Output) -> None:
        with output.lock:
            if output.stream is not None and not output.finished.is_set():
                try:
                    output.stream.abort()
                except (OSError, sd.PortAudioError) as exc:
                    raise HalError(f"停止音频失败: {exc}") from exc

    def _play_blocking(self, data: bytes, output: _Output) -> None:
        try:
            with sd.RawOutputStream(
                device=self.device,
                samplerate=self.samplerate,
                channels=1,
                dtype="int16",
            ) as stream:
                with output.lock:
                    output.stream = stream
                # Small writes bound buffered output and allow stop before next chunk.
                chunk_bytes = max(2, int(self.samplerate * 0.02) * 2)
                for start in range(0, len(data), chunk_bytes):
                    if output.stopped.is_set():
                        break
                    stream.write(data[start:start + chunk_bytes])
                if output.stopped.is_set():
                    stream.abort()
                with output.lock:
                    output.stream = None
        except (OSError, sd.PortAudioError) as exc:
            if not output.stopped.is_set():
                raise HalError(f"播放失败: {exc}") from exc
        finally:
            with output.lock:
                output.stream = None
                output.finished.set()
