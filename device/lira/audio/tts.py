"""TTS 语音合成（U2）：sherpa-onnx OfflineTts（matcha zh-baker + vocos vocoder）。

要点（计划 U2 Approach / Key Decisions）：
  - rule FST（number/date/phone.fst）保证数字、日期、号码被正确读出
    （"2026年10月1日" 不读成 "二零二六年一零月一日"）。
  - `speak(text, speed, volume, owner=...)` 返回带状态的完成事件。
  - 提示音与合成请求串行输出；取消所有者后停止当前硬件输出。
  - occupied/released hook 只报告输出生命周期，不要求丢弃麦克风输入。
  - 播放经注入的 AudioIO（x86 无声卡测试用 MockAudioIO 记内存；
    实机/板上注入 SoundDevice 播放器）。
"""

from __future__ import annotations

import asyncio
import os
import re
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import sherpa_onnx

from lira.audio._paths import DOWNLOAD_HINT, TTS_MODEL_DIR, VOCODER_ONNX
from lira.hal import AudioIO
from lira.audio.speech_text import spoken_text

__all__ = ["TtsEngine", "PlaybackCompletion"]


class PlaybackCompletion(asyncio.Event):
    """Event-compatible completion, including the actual output outcome."""

    def __init__(self, owner: object = None) -> None:
        super().__init__()
        self.owner = owner
        self.status = "pending"
        self.error: Exception | None = None

    def finish(self, status: str, error: Exception | None = None) -> None:
        if not self.is_set():
            self.status, self.error = status, error
            self.set()


@dataclass
class _PlaybackRequest:
    done: PlaybackCompletion
    text: str | None
    speed: float
    volume: float
    cancelled: bool = False
    playing: bool = False
    abort: asyncio.Task | None = None

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
        backend: str | None = None,
    ) -> None:
        backend = backend or os.environ.get("LIRA_TTS_BACKEND", "matcha")
        num_threads = int(os.environ.get("LIRA_TTS_THREADS", str(num_threads)))
        if backend not in ("matcha", "kokoro", "melo"):
            raise ValueError("LIRA_TTS_BACKEND must be matcha, kokoro or melo")
        self._backend = backend
        self._speaker_zh = int(os.environ.get("LIRA_TTS_SPEAKER_ZH", "3"))
        self._speaker_en = int(os.environ.get("LIRA_TTS_SPEAKER_EN", "0"))
        if backend == "kokoro" and Path(model_dir) == TTS_MODEL_DIR:
            model_dir = TTS_MODEL_DIR.parent / "kokoro-multi-lang-v1_1"
        if backend == "melo" and Path(model_dir) == TTS_MODEL_DIR:
            model_dir = TTS_MODEL_DIR.parent / "vits-melo-tts-zh_en"
        model_dir = Path(model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"TTS 模型目录不存在: {model_dir}。{DOWNLOAD_HINT}")

        acoustic = _pick_onnx(model_dir, "model")
        rule_names = ("phone-zh.fst", "date-zh.fst", "number-zh.fst") if backend == "kokoro" else _RULE_FSTS
        rule_fsts = ",".join(
            str(model_dir / f) for f in rule_names if (model_dir / f).is_file()
        )

        if backend == "melo":
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=str(acoustic), lexicon=str(model_dir / "lexicon.txt"),
                        tokens=str(model_dir / "tokens.txt"), dict_dir=str(model_dir / "dict")),
                    num_threads=num_threads), rule_fsts=rule_fsts)
            self._english_engine = TtsEngine(backend="kokoro", num_threads=num_threads)
        elif backend == "kokoro":
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=str(acoustic), voices=str(model_dir / "voices.bin"),
                        tokens=str(model_dir / "tokens.txt"),
                        data_dir=str(model_dir / "espeak-ng-data"),
                        lexicon=",".join(str(model_dir / f) for f in
                                         ("lexicon-us-en.txt", "lexicon-zh.txt")),
                        dict_dir=str(model_dir / "dict"),
                    ), num_threads=num_threads,
                ), rule_fsts=rule_fsts,
            )
        else:
            vocoder = _resolve_vocoder(Path(vocoder))
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
        self._init_playback()
        logging.info(
            "TTS 已加载: %s (rule_fsts=%s, sr=%d)",
            model_dir.name,
            rule_fsts,
            self.sample_rate,
        )

    def generate(self, text: str, speed: float = 1.0) -> np.ndarray:
        """同步合成，返回 float32 波形（模型采样率）。"""
        if self._backend == "melo" and re.search(r'[A-Za-z]', text):
            samples = self._english_engine.generate(text, speed)
            ratio = self.sample_rate / self._english_engine.sample_rate
            return np.interp(np.arange(int(len(samples)*ratio))/ratio,
                             np.arange(len(samples)), samples).astype(np.float32)
        sid = 0
        if self._backend == "kokoro":
            sid = self._speaker_zh if re.search(r"[\u3400-\u9fff]", text) else self._speaker_en
        audio = self.tts.generate(text, sid=sid, speed=speed)
        return np.asarray(audio.samples, dtype=np.float32)

    def _init_playback(self) -> None:
        self._queue: deque[_PlaybackRequest] = deque()
        self._worker: asyncio.Task | None = None
        self._active: _PlaybackRequest | None = None
        self._closed = False

    def speak(self, text: str, speed: float = 1.0, volume: float = 1.0,
              *, owner: object = None) -> PlaybackCompletion:
        return self._enqueue(text, speed, volume, owner)

    def chime(self, *, owner: object = None, volume: float = 1.0) -> PlaybackCompletion:
        """Short wake feedback through the same serialized output path."""
        return self._enqueue(None, 1.0, volume, owner)

    def _enqueue(self, text, speed, volume, owner) -> PlaybackCompletion:
        done = PlaybackCompletion(owner)
        if self._closed:
            done.finish("cancelled")
            return done
        self._queue.append(_PlaybackRequest(done, text, speed, volume))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
        return done

    def cancel_owner(self, owner: object) -> None:
        """Invalidate now; active output is aborted before a successor can play."""
        self._cancel_matching(lambda request: request.done.owner == owner)

    def _cancel_matching(self, matches) -> None:
        kept = deque()
        for request in self._queue:
            if matches(request):
                request.cancelled = True
                request.done.finish("cancelled")
            else:
                kept.append(request)
        self._queue = kept
        request = self._active
        if request is not None and matches(request):
            request.cancelled = True
            if request.playing and request.abort is None:
                request.abort = asyncio.create_task(self._abort_output(request))
            elif not request.playing:
                # Synthesis cannot be stopped, but it can no longer emit output.
                request.done.finish("cancelled")

    async def _abort_output(self, request: _PlaybackRequest) -> None:
        if self._active is request and request.playing and self._player is not None:
            try:
                await self._player.stop()
            except Exception as exc:
                # Do not claim silence or let a successor play on unconfirmed stop.
                request.done.finish("failed", exc)
                raise

    async def stop(self, owner: object = None) -> None:
        """Invalidate selected requests and wait for actual output termination."""
        request = self._active
        self._cancel_matching(lambda item: owner is None or item.done.owner == owner)
        if request is not None and (owner is None or request.done.owner == owner):
            if request.abort is not None:
                await asyncio.shield(request.abort)
                await request.done.wait()

    async def close(self) -> None:
        self._closed = True
        await self.stop()
        if self._worker is not None:
            await asyncio.shield(self._worker)
        if self._player is not None:
            await self._player.close()

    async def _drain(self) -> None:
        while self._queue:
            request = self._active = self._queue.popleft()
            pending_synthesis = None
            try:
                self.occupied = True
                self._notify(self._on_occupied)
                if request.text is None:
                    t = np.arange(int(self.sample_rate * 0.08)) / self.sample_rate
                    samples = (0.18 * np.sin(2 * np.pi * 880 * t)
                               * np.sin(np.pi * np.arange(len(t)) / len(t)) ** 2)
                else:
                    chunks = list(speech_chunks(spoken_text(request.text)))
                    for index, chunk in enumerate(chunks):
                        if request.cancelled:
                            break
                        started = time.monotonic()
                        if pending_synthesis is None:
                            samples = await asyncio.to_thread(self.generate, chunk, request.speed)
                        else:
                            samples = await pending_synthesis
                            pending_synthesis = None
                        logging.info("tts event=chunk_ready chars=%d elapsed=%.3f", len(chunk), time.monotonic()-started)
                        if not request.cancelled and index + 1 < len(chunks):
                            pending_synthesis = asyncio.create_task(asyncio.to_thread(
                                self.generate, chunks[index+1], request.speed))
                        if not request.cancelled and self._player is not None:
                            request.playing = True
                            await self._player.play(to_pcm16(samples, request.volume))
                            request.playing = False
                        if request.abort is not None:
                            await request.abort
                        if request.cancelled:
                            break
                    request.done.finish("cancelled" if request.cancelled else "success")
                    continue
                if not request.cancelled and self._player is not None:
                    request.playing = True
                    await self._player.play(to_pcm16(samples, request.volume))
                if request.abort is not None:
                    await request.abort
                request.done.finish("cancelled" if request.cancelled else "success")
            except Exception as exc:
                logging.exception("Audio output failed")
                request.done.finish("failed", exc)
            finally:
                # A cancelled reply must not leave a synth worker racing its successor.
                if pending_synthesis is not None:
                    await asyncio.gather(pending_synthesis, return_exceptions=True)
                # An abort must finish before a new request acquires the player.
                if request.abort is not None:
                    try:
                        await request.abort
                    except Exception as exc:
                        request.done.finish("failed", exc)
                self._active = None
                self.occupied = False
                self._notify(self._on_released)

    @staticmethod
    def _notify(callback: Callable[[], None] | None) -> None:
        if callback is not None:
            try:
                callback()
            except Exception:
                logging.exception("Audio lifecycle callback failed")


def to_pcm16(samples: np.ndarray, volume: float = 1.0) -> bytes:
    """float32 [-1,1] 波形 → 音量缩放后的 16bit PCM 字节（HAL play 输入）。"""
    scaled = np.clip(samples * volume, -1.0, 1.0)
    return (scaled * 32767.0).astype(np.int16).tobytes()


def speech_chunks(text: str):
    """Bound startup synthesis without splitting decimal numbers or English words."""
    remaining = text.strip()
    first = True
    while remaining:
        limit = 24 if first else 32
        if len(remaining) <= limit:
            yield remaining
            return
        head = remaining[:limit]
        boundaries = list(re.finditer(r"[。！？!?；;，,]|\.(?=\s)", head))
        if boundaries:
            useful = [b for b in boundaries if b.end() >= 10]
            end = (useful[0] if first else useful[-1]).end() if useful else limit
        elif " " in head:
            end = head.rfind(" ") + 1
        else:
            end = limit
            # Keep a long English token intact.
            while end < len(remaining) and remaining[end-1:end+1].isascii() and remaining[end-1:end+1].isalnum():
                end += 1
        while end < len(remaining) and remaining[end-1:end+1].isascii() and remaining[end-1:end+1].isalnum():
            end += 1
        yield remaining[:end].strip()
        remaining = remaining[end:].lstrip()
        first = False


def _pick_onnx(model_dir: Path, prefix: str) -> Path:
    candidates = sorted(model_dir.glob(f"{prefix}*.onnx"))
    full = [p for p in candidates if "int8" not in p.name]
    if not candidates:
        raise FileNotFoundError(f"{model_dir} 中找不到 {prefix}*.onnx。{DOWNLOAD_HINT}")
    return (full or candidates)[-1]
