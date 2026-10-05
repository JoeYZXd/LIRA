"""Continuous cleaned-audio dispatch, wake handoff and bounded in-memory pre-roll."""
from __future__ import annotations

from collections import deque
import numpy as np

from lira.audio.activity import SpeechActivity
from lira.dialog.state_machine import AudioRoute
from lira.voice_log import VOICE_LOG


class RouteDispatch:
    def __init__(self, *, streams, wakeword: str, spawn) -> None:
        self._streams, self._wakeword, self._spawn = streams, wakeword, spawn
        self._ring = deque()
        self._next_sample = 0
        self._fed_until = 0
        self._max_samples = 3 * 16000
        self._activity = SpeechActivity()
        self._generation = 0
        self._wake_pending = False
        self._last_route = streams.route
        self._parts = []
        self._segment_samples = 0

    @property
    def generation(self):
        return self._generation

    def clear(self) -> None:
        self._generation += 1
        self._ring.clear()
        self._fed_until = self._next_sample
        self._activity.reset()
        self._parts.clear()
        self._segment_samples = 0
        self._wake_pending = False
        for stream in (self._streams.asr_stream, self._streams.wake_stream,
                       self._streams.playback_stream):
            if stream is not None and hasattr(stream, "reset"):
                stream.reset()

    def apply_privacy(self, enabled: bool) -> None:
        self.clear()

    def begin_listening(self, *, replay: bool = True) -> None:
        self._generation += 1
        if self._streams.asr_stream is not None:
            self._streams.asr_stream.reset()
        self._activity.reset()
        self._parts.clear()
        self._segment_samples = 0
        self._wake_pending = False
        self._last_route = AudioRoute.LISTEN
        # The event loop cannot interleave another feed with this synchronous snapshot.
        snapshot = list(self._ring) if replay else []
        self._fed_until = snapshot[0][0] if snapshot else self._next_sample
        for start, samples in snapshot:
            self._feed_asr(start, samples)

    def _event(self, name):
        callback = getattr(self._streams.engine, name, None)
        if callback is not None:
            result = callback(self._generation)
            if result is not None:
                self._spawn(result)

    def _feed_asr(self, start, samples):
        end = start + len(samples)
        if end <= self._fed_until:
            return
        samples = samples[max(0, self._fed_until - start):]
        self._fed_until = end
        stream = self._streams.asr_stream
        if stream is None:
            return
        for event in self._activity.feed(samples):
            self._event("on_speech_" + event)
        self._event("on_audio_progress")
        stream.feed(samples)
        self._segment_samples += len(samples)
        text = stream.text().strip()
        endpoint = stream.is_endpoint()
        # Segment long speech for bounded recognizer memory, without submitting an
        # unfinished user request. Only real silence allows delivery.
        if self._segment_samples >= 20 * 16000 or (endpoint and self._activity.active):
            if text:
                self._parts.append(text)
            stream.reset()
            self._segment_samples = 0
            return
        if endpoint and not self._activity.active:
            text = "".join(self._parts) + text
            self._parts.clear()
            stream.reset()
            self._segment_samples = 0
            if text.startswith(self._wakeword):
                text = text[len(self._wakeword):].lstrip(" ，,。.!！?？、")
            if text:
                VOICE_LOG.log("asr", f"识别到指令：{text}")
                self._deliver(text)
            else:
                self._event("on_empty_endpoint")

    def _deliver(self, text):
        generation = self._generation
        async def deliver():
            if generation != self._generation:
                return
            engine = self._streams.engine
            if hasattr(engine, "listening_generation"):
                await engine.on_asr_text(text, generation=generation)
            else:
                await engine.on_asr_text(text)
        self._spawn(deliver())

    def feed(self, samples) -> None:
        rt = self._streams
        if isinstance(samples, bytes):
            samples = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            samples = np.asarray(samples, dtype=np.float32)
        route = rt.route
        if route is AudioRoute.LISTEN and self._last_route is not AudioRoute.LISTEN:
            self.begin_listening(replay=self._wake_pending)
        if route is AudioRoute.WAKE and self._last_route is not AudioRoute.WAKE:
            self.clear()
        self._last_route = route
        start = self._next_sample
        self._next_sample += len(samples)
        self._ring.append((start, samples.copy()))
        cutoff = self._next_sample - self._max_samples
        while self._ring and self._ring[0][0] + len(self._ring[0][1]) <= cutoff:
            self._ring.popleft()
        if self._ring and self._ring[0][0] < cutoff:
            old_start, old = self._ring.popleft()
            self._ring.appendleft((cutoff, old[cutoff-old_start:].copy()))
        # KWS stays active while listening, processing and playing. AEC provides
        # self-audio rejection; occupied TTS must never discard cleaned input.
        hit = rt.wake_stream.feed_and_poll(samples) if rt.wake_stream is not None else None
        if hit and self._wakeword in hit and not self._wake_pending:
            self._wake_pending = True
            VOICE_LOG.log("wake", f"收到语音唤醒（{self._wakeword}）")
            generation = self._generation
            async def wake():
                if generation == self._generation:
                    await rt.engine.on_wake()
            self._spawn(wake())
            return
        if route is AudioRoute.LISTEN and not self._wake_pending:
            self._feed_asr(start, samples)
        elif route is AudioRoute.PLAYBACK and rt.playback_stream is not None and not self._wake_pending:
            hit = rt.playback_stream.feed_and_poll(samples)
            if hit:
                word = hit.split("@")[-1].strip()
                VOICE_LOG.log("playback", f"播放控制语音：{word}")
                self._deliver(word)
