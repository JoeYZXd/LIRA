"""麦克风采集与分发（U2）：单一音频源 → KWS / ASR 多路订阅。

设计要点（计划 Key Decisions）：
  - sounddevice 单实例采集，避免独占冲突；板上/实机共用 `SoundDeviceMic`。
  - 无声卡环境（CI / 纯离线测试）注入任意 `AudioIO` 源（如 MockAudioIO wav）。
  - `MicDistributor` 负责把 16bit PCM 转 float32 后广播给全部 sink，并按 sink
    计帧，供测试断言"双流分发不丢帧"（Test scenario 4）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import numpy as np

from lira.audio._paths import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH
from lira.hal import AudioIO

#: 默认分发块长：0.1s = 1600 帧 = 3200 字节（KWS 增量延迟与 CPU 占用的折中）
CHUNK_FRAMES = 1600

__all__ = ["CHANNELS", "CHUNK_FRAMES", "SAMPLE_RATE", "SAMPLE_WIDTH", "MicDistributor", "SoundDeviceMic", "AudioSink"]


class AudioSink(Protocol):
    """分发目标协议：KWS / ASR 流包装均实现 feed()。"""

    def feed(self, samples: np.ndarray) -> None:
        """接收一段 float32 波形（[-1, 1] 归一化，16kHz 单声道）。"""


class SoundDeviceMic(AudioIO):
    """sounddevice 单实例麦克风采集（16kHz/mono/int16）。

    通过后台线程回调把数据搬进 asyncio.Queue，供事件循环以
    `read_chunk()` 消费——与 MockAudioIO 同一接口，MicDistributor 无感切换。
    """

    def __init__(
        self,
        samplerate: int = SAMPLE_RATE,
        blocksize: int = CHUNK_FRAMES,
        device: int | str | None = None,
    ) -> None:
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._device = device
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._stream: object | None = None

    async def open(self) -> None:
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue = self._queue

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001 - sd 回调签名
            if status:
                logging.warning("麦克风溢出/状态异常: %s", status)
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

        self._stream = sd.InputStream(
            samplerate=self._samplerate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=callback,
        )
        self._stream.start()
        logging.info("sounddevice 采集已启动: %s Hz @ %s", self._samplerate, self._device)

    async def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def read_chunk(self, size: int) -> bytes:
        """阻塞取一块采集数据（真实麦克风是无限流，不会返回 b""）。"""
        return await self._queue.get()

    async def play(self, data: bytes) -> None:
        """采集端不播放；TTS 播放走独立输出设备（见 tts.py player 注入）。"""
        raise NotImplementedError("SoundDeviceMic 仅采集；播放请注入独立 player")


class MicDistributor:
    """把一个 AudioIO 源的 PCM 块广播给全部 sink，并按 sink 统计帧数。

    用法::

        dist = MicDistributor(audio_source)
        dist.add_sink(kws_stream)   # WakeWordKws.KwsStream
        dist.add_sink(asr_stream)   # StreamingAsr.AsrStream
        await dist.run()            # 阻塞到源 EOF（wav 注入）或任务取消（麦克风）
    """

    def __init__(self, source: AudioIO, chunk_frames: int = CHUNK_FRAMES) -> None:
        self._source = source
        self._chunk_frames = chunk_frames
        self.sinks: list[AudioSink] = []
        #: 源侧总帧数（EOF 后即输入总长，供无丢帧断言）
        self.frames_total = 0
        #: 各 sink 实收帧数，与 frames_total 一一相等即无丢帧
        self.frames_per_sink: list[int] = []

    def add_sink(self, sink: AudioSink) -> None:
        self.sinks.append(sink)
        self.frames_per_sink.append(0)

    async def run(self) -> None:
        """持续读取并分发，直到源返回 b""（EOF）或被取消。"""
        while True:
            chunk = await self._source.read_chunk(self._chunk_frames * SAMPLE_WIDTH)
            if not chunk:
                break
            if len(chunk) % SAMPLE_WIDTH:  # 截断保护：保证 int16 对齐
                chunk = chunk[:-1]
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            self.frames_total += len(samples)
            for i, sink in enumerate(self.sinks):
                sink.feed(samples)
                self.frames_per_sink[i] += len(samples)

    async def run_in_background(self) -> asyncio.Task[None]:
        return asyncio.create_task(self.run())
