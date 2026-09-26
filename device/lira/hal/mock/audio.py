"""mock 音频：从 wav 文件读（模拟麦克风流），播放记录到内存。"""

from __future__ import annotations

import wave
from pathlib import Path

from lira.hal.base import AudioIO, HalError

# 项目统一 16kHz 单声道 16bit PCM；mock 从 wav 读取时按文件实际参数交出原始帧，
# 上层（U2 音频管线）负责重采样/校验。
_SAMPLE_WIDTH_BYTES = 2


class MockAudioIO(AudioIO):
    """wav 文件注入式音频 I/O。

    Args:
        wav_path: 作为麦克风输入的 wav 文件。open() 时打开，read_chunk()
            按帧交出 PCM；读完返回 b""。
    """

    def __init__(self, wav_path: Path) -> None:
        self.wav_path = Path(wav_path)
        self._wave_file: wave.Wave_read | None = None
        self.sample_rate = 0
        self.channels = 0
        self.played: list[bytes] = []

    async def open(self) -> None:
        if not self.wav_path.is_file():
            raise HalError(f"mock 音频 wav 文件不存在: {self.wav_path}")
        try:
            self._wave_file = wave.open(str(self.wav_path), "rb")
        except wave.Error as exc:
            raise HalError(f"mock 音频 wav 文件无法解析: {self.wav_path} ({exc})") from exc
        self.sample_rate = self._wave_file.getframerate()
        self.channels = self._wave_file.getnchannels()
        self.played = []

    async def close(self) -> None:
        if self._wave_file is not None:
            self._wave_file.close()
            self._wave_file = None

    async def read_chunk(self, size: int) -> bytes:
        """读取 `size` 字节 PCM；流末尾返回 b""。"""
        if self._wave_file is None:
            raise HalError("mock 音频未打开（请用作 async 上下文管理器）。")
        if size <= 0:
            raise HalError("read_chunk size 必须为正数。")
        data = self._wave_file.readframes(
            size // (_SAMPLE_WIDTH_BYTES * max(self.channels, 1))
        )
        return data

    async def play(self, data: bytes) -> None:
        """播放 = 记录到内存 `played` 列表（测试断言 TTS 播报副作用用）。"""
        self.played.append(bytes(data))
