"""TTS 语音合成（U2）：sherpa-onnx OfflineTts（matcha zh-baker + vocos vocoder）。

要点（计划 U2 Approach / Key Decisions）：
  - rule FST（number/date/phone.fst）保证数字、日期、号码被正确读出
    （"2026年10月1日" 不读成 "二零二六年一零月一日"）。
  - `speak(text, speed, volume)` 返回完成事件：事件 set 即播放结束。
  - 半双工保护 hook：播放开始广播 occupied、结束广播 released，
    供 U3 状态机关闭全量 ASR/唤醒 KWS、只留播放白名单 KWS（AE4 结构性保证）。
  - 播放经注入的 AudioIO（x86 无声卡测试用 MockAudioIO 记内存；
    实机/板上注入 SoundDevice 播放器）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

import numpy as np
import sherpa_onnx

from lira.audio._paths import DOWNLOAD_HINT, TTS_MODEL_DIR, VOCODER_ONNX
from lira.hal import AudioIO

__all__ = ["TtsEngine"]

#: 规则 FST 优先级顺序（后挂的先匹配会被前面的覆盖，号码放最前最具体）
_RULE_FSTS = ("phone.fst", "date.fst", "number.fst")


def _resolve_vocoder(vocoder: Path) -> Path:
    """vocoder 入参兼容裸 onnx 文件（vocoder-models release）或目录。"""
    if vocoder.is_dir():
        return _pick_onnx(vocoder, "vocos")
    if not vocoder.is_file():
        raise FileNotFoundError(f"vocoder 不存在: {vocoder}。{DOWNLOAD_HINT}")
    return vocoder


class TtsEngine:
    """OfflineTts 封装：合成 +（可选）播放 + 完成/占用事件。"""

    def __init__(
        self,
        model_dir: str | Path = TTS_MODEL_DIR,
        vocoder: str | Path = VOCODER_ONNX,
        player: AudioIO | None = None,
        on_occupied: Callable[[], None] | None = None,
        on_released: Callable[[], None] | None = None,
        num_threads: int = 2,
    ) -> None:
        model_dir = Path(model_dir)
        vocoder = _resolve_vocoder(Path(vocoder))
        if not model_dir.is_dir():
            raise FileNotFoundError(f"TTS 模型目录不存在: {model_dir}。{DOWNLOAD_HINT}")

        acoustic = _pick_onnx(model_dir, "model")
        rule_fsts = ",".join(
            str(model_dir / f) for f in _RULE_FSTS if (model_dir / f).is_file()
        )

        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                    acoustic_model=str(acoustic),
                    vocoder=str(vocoder),
                    lexicon=str(model_dir / "lexicon.txt"),
                    tokens=str(model_dir / "tokens.txt"),
                    dict_dir=str(model_dir / "dict"),
                ),
                num_threads=num_threads,
            ),
            rule_fsts=rule_fsts,
        )
        self.tts = sherpa_onnx.OfflineTts(config)
        self.sample_rate: int = self.tts.sample_rate
        self._player = player
        self._on_occupied = on_occupied
        self._on_released = on_released
        #: 正在播放标志（半双工：状态机可轮询；hook 回调同时广播）
        self.occupied = False
        logging.info(
            "TTS 已加载: %s + vocos (rule_fsts=%s, sr=%d)",
            model_dir.name,
            rule_fsts,
            self.sample_rate,
        )

    def generate(self, text: str, speed: float = 1.0) -> np.ndarray:
        """同步合成，返回 float32 波形（模型采样率）。"""
        audio = self.tts.generate(text, sid=0, speed=speed)
        return np.asarray(audio.samples, dtype=np.float32)

    def speak(self, text: str, speed: float = 1.0, volume: float = 1.0) -> asyncio.Event:
        """合成并播放（后台任务），返回完成事件（set 即播放结束）。

        合成在 to_thread 中执行，不阻塞事件循环；volume 为 int16 满幅的缩放系数。
        """
        done = asyncio.Event()
        asyncio.get_running_loop().create_task(self._speak(text, speed, volume, done))
        return done

    async def _speak(self, text: str, speed: float, volume: float, done: asyncio.Event) -> None:
        try:
            self.occupied = True
            if self._on_occupied:
                self._on_occupied()
            samples = await asyncio.to_thread(self.generate, text, speed)
            pcm = to_pcm16(samples, volume)
            if self._player is not None:
                await self._player.play(pcm)
        finally:
            self.occupied = False
            if self._on_released:
                self._on_released()
            done.set()


def to_pcm16(samples: np.ndarray, volume: float = 1.0) -> bytes:
    """float32 [-1,1] 波形 → 音量缩放后的 16bit PCM 字节（HAL play 输入）。"""
    scaled = np.clip(samples * volume, -1.0, 1.0)
    return (scaled * 32767.0).astype(np.int16).tobytes()


def _pick_onnx(model_dir: Path, prefix: str) -> Path:
    candidates = sorted(model_dir.glob(f"{prefix}*.onnx"))
    full = [p for p in candidates if "int8" not in p.name]
    if not candidates:
        raise FileNotFoundError(f"{model_dir} 中找不到 {prefix}*.onnx。{DOWNLOAD_HINT}")
    return (full or candidates)[-1]
