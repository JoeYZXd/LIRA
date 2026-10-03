"""板上播放器 HAL（U10）：sounddevice 输出（ES8388 → 3.5mm → 有源音箱）。

TTS 以 matcha 模型采样率合成后送 `play()`；输出设备默认走 PipeWire 默认
sink（板载 ES8388 模拟口），软音量在播报层实现（R21"大声点/小声点"）。
采集侧直接复用 `lira.audio.mic.SoundDeviceMic`（板上/实机共用，U2 交付）。
"""

from __future__ import annotations

import asyncio

import sounddevice as sd

from lira.hal.base import AudioIO, HalError

__all__ = ["SoundDevicePlayer"]


class SoundDevicePlayer(AudioIO):
    """`AudioIO` 播放侧实现：每次 play 开一次性 OutputStream（块级生命周期）。"""

    def __init__(self, device: int | str | None = None, samplerate: int = 22050) -> None:
        self.device = device
        self.samplerate = int(samplerate)

    async def open(self) -> None:
        """探测输出设备（默认 sink 存在即通过）。"""
        try:
            sd.check_output_settings(
                device=self.device, samplerate=self.samplerate,
                channels=1, dtype="int16",
            )
        except OSError as exc:
            raise HalError(f"音频输出设备不可用: {exc}") from exc

    async def close(self) -> None:
        pass

    async def read_chunk(self, size: int) -> bytes:
        raise NotImplementedError("SoundDevicePlayer 仅播放，不采集")

    async def play(self, data: bytes) -> None:
        """播放 int16 单声道 PCM（阻塞写在线程池，不卡事件循环）。"""
        await asyncio.to_thread(self._play_blocking, data)

    def _play_blocking(self, data: bytes) -> None:
        try:
            with sd.RawOutputStream(
                device=self.device,
                samplerate=self.samplerate,
                channels=1,
                dtype="int16",
            ) as stream:
                stream.write(data)
        except OSError as exc:
            raise HalError(f"播放失败: {exc}") from exc
