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
import itertools
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import sherpa_onnx

from lira.audio._paths import DOWNLOAD_HINT, TTS_MODEL_DIR, VOCODER_ONNX
from lira.hal import AudioIO
from lira.audio.speech_text import spoken_text
from lira.audio.speech_session import SpeechSession
from lira.audio.voice_metrics import current_trace, current_answer, mark

__all__ = ["TtsEngine", "PlaybackCompletion"]
_METRIC_SEGMENTS = itertools.count()


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
    trace: object = None
    answer: bool = False
    cancelled: bool = False
    playing: bool = False
    abort: asyncio.Task | None = None
    session: SpeechSession | None = None
    network: asyncio.Task | None = None
    replay: list | None = None

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
        self._online = None
        self._online_disabled = False
        self._online_allowed = lambda: False

    def configure_online(self, config, *, allowed):
        from lira.audio.tts_volcengine import VolcengineProvider
        self._online = VolcengineProvider(config) if config.ready else None
        self._online_allowed = allowed

    @property
    def accepts_text_deltas(self):
        return self._online is not None and not self._online_disabled

    async def apply_privacy(self, enabled):
        if enabled:
            await self.stop()

    async def _play_online(self, request):
        replay, count, replayable = [], 0, True
        baseline = getattr(self._player, 'output_frames', None)
        def allowed():
            return not request.cancelled and self._online_allowed()
        async def text():
            nonlocal count, replayable
            async for delta in request.session.fragments():
                if not allowed():
                    raise asyncio.CancelledError()
                if replayable:
                    count += len(delta)
                    if count <= 256:
                        replay.append(delta)
                    else:
                        replay.clear()
                        replayable = False
                yield delta
        async def pcm():
            source = self._online.stream(text(), sample_rate=self.sample_rate,
                                         speed=request.speed, allowed=allowed)
            try:
                async for data in source:
                    if not allowed():
                        raise asyncio.CancelledError()
                    samples = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768
                    yield to_pcm16(samples, request.volume)
            finally:
                await source.aclose()
        source = pcm()
        request.playing = True
        request.network = asyncio.create_task(self._player.play_stream(source))
        try:
            await request.network
            if count and baseline is not None and self._player.output_frames == baseline:
                raise RuntimeError("online speech produced no audio")
            return True
        except asyncio.CancelledError:
            request.cancelled = True
            request.session.cancel()
            await self._player.stop()
            return True
        except Exception:
            self._online_disabled = True
            await self._player.stop()
            emitted = baseline is None or self._player.output_frames != baseline
            if emitted or not replayable or not allowed():
                raise RuntimeError('online speech failed') from None
            request.replay = replay
            return False
        finally:
            request.network = None
            request.playing = False
            await source.aclose()

    def start_session(self, speed: float = 1.0, volume: float = 1.0,
                      *, owner: object = None) -> SpeechSession:
        done = self._enqueue("", speed, volume, owner)
        session = SpeechSession(done)
        if not done.is_set():
            self._queue[-1].session = session
        return session

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
        if len(self._queue) >= 16:
            done.finish("failed", RuntimeError("speech request capacity exceeded"))
            return done
        trace = current_trace.get()
        mark(trace, "segment_submit", answer=int(current_answer.get()))
        self._queue.append(_PlaybackRequest(done, text, speed, volume, trace=trace, answer=current_answer.get()))
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
                mark(request.trace, "cancel_requested")
                request.cancelled = True
                if request.session is not None:
                    request.session.cancel()
                request.done.finish("cancelled")
            else:
                kept.append(request)
        self._queue = kept
        request = self._active
        if request is not None and matches(request):
            mark(request.trace, "cancel_requested")
            request.cancelled = True
            if request.network is not None:
                request.network.cancel()
            if request.session is not None:
                request.session.cancel()
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

    def _generate_measured(self, request, chunk, segment):
        segment = next(_METRIC_SEGMENTS)
        started = time.monotonic()
        mark(request.trace, "synthesis_start", segment=segment, at=started)
        samples = self.generate(chunk, request.speed)
        ended = time.monotonic()
        mark(request.trace, "synthesis_end", segment=segment, at=ended,
             audio_seconds=len(samples) / self.sample_rate)
        mark(request.trace, "pcm_ready", segment=segment, frames=len(samples))
        logging.info("tts event=synthesis_ready chars=%d elapsed=%.3f", len(chunk), ended-started)
        return samples

    async def _fallback_fragments(self, request):
        # Only online fallback needs local segmentation: ordinary local input
        # has already passed through this same incremental segmenter upstream.
        from lira.audio.text_stream import consume_text
        queue = asyncio.Queue(maxsize=1)
        replay, request.replay = request.replay, None
        async def source():
            for delta in replay:
                yield delta
            async for delta in request.session.fragments():
                yield delta
        async def segment():
            try:
                await consume_text(source(), queue.put, max_buffer_chars=256)
            finally:
                if not asyncio.current_task().cancelling():
                    await queue.put(None)
        task = asyncio.create_task(segment())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    await task
                    return
                yield chunk
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _request_fragments(self, request):
        if request.replay is not None:
            async for chunk in self._fallback_fragments(request):
                yield chunk
            return
        if request.session is not None:
            async for text in request.session.fragments():
                cleaned = spoken_text(text)
                if cleaned:
                    yield cleaned
        else:
            for chunk in speech_chunks(spoken_text(request.text)):
                yield chunk

    async def _drain(self) -> None:
        while self._queue:
            request = self._active = self._queue.popleft()
            self.occupied = True
            self._notify(self._on_occupied)
            pending = None
            async def synthesize_next(iterator):
                try:
                    chunk = await anext(iterator)
                except StopAsyncIteration:
                    return None
                if request.cancelled:
                    return None
                return await asyncio.to_thread(self._generate_measured, request, chunk, 0)

            async def pcm_source(first, iterator):
                nonlocal pending
                samples = first
                while samples is not None and not request.cancelled:
                    if iterator is not None:
                        pending = asyncio.create_task(synthesize_next(iterator))
                    pcm = to_pcm16(samples, request.volume)
                    # HAL accepts at most two seconds per source chunk.
                    limit = self.sample_rate * 2 * 2
                    for offset in range(0, len(pcm), limit):
                        if request.cancelled:
                            return
                        yield pcm[offset:offset + limit]
                    samples = await asyncio.shield(pending) if pending is not None else None
                    pending = None

            try:
                iterator = None
                if (request.session is not None and self.accepts_text_deltas
                        and self._player is not None and self._online_allowed()):
                    if await self._play_online(request):
                        request.done.finish('cancelled' if request.cancelled else 'success')
                        continue
                if request.text is None:
                    t = np.arange(int(self.sample_rate * 0.08)) / self.sample_rate
                    samples = (0.18 * np.sin(2 * np.pi * 880 * t)
                               * np.sin(np.pi * np.arange(len(t)) / len(t)) ** 2)
                else:
                    iterator = self._request_fragments(request)
                    samples = await synthesize_next(iterator)
                if samples is not None and not request.cancelled:
                    if self._player is not None:
                        request.playing = True
                        token = current_trace.set(request.trace)
                        answer_token = current_answer.set(request.answer)
                        source = pcm_source(samples, iterator)
                        try:
                            # Legacy injected test doubles only; production HALs
                            # provide the continuous stream implementation.
                            if hasattr(self._player, "play_stream"):
                                await self._player.play_stream(source)
                            else:
                                async for pcm in source:
                                    await self._player.play(pcm)
                        finally:
                            await source.aclose()
                            current_trace.reset(token)
                            current_answer.reset(answer_token)
                        request.playing = False
                    else:
                        async for _ in pcm_source(samples, iterator):
                            pass
                if request.abort is not None:
                    await request.abort
                request.done.finish("cancelled" if request.cancelled else "success")
            except Exception as exc:
                logging.exception("Audio output failed")
                request.done.finish("failed", exc)
            finally:
                if request.session is not None:
                    request.session.cancel()
                # Never cancel native inference: successors share this model.
                if pending is not None:
                    await asyncio.gather(pending, return_exceptions=True)
                if iterator is not None:
                    await iterator.aclose()
                if request.abort is not None:
                    try:
                        await request.abort
                    except Exception as exc:
                        request.done.finish("failed", exc)
                if request.cancelled:
                    mark(request.trace, "cancel_confirmed")
                if request.session is not None:
                    request.session.cancel()
                self._active = None
                if self.occupied:
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
