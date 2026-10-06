"""Continuous cleaned-audio dispatch, wake handoff and bounded in-memory pre-roll."""
from __future__ import annotations

from collections import deque
import numpy as np

from lira.audio.activity import SpeechActivity
from lira.dialog.intents import COMMAND_KWS_WORDS
from lira.dialog.state_machine import AudioRoute, State
from lira.voice_log import VOICE_LOG

_SAMPLE_RATE = 16000

#: TTS 释放后的吞弃窗：AEC 清洗输出有对齐延迟，播放结束后其尾音仍会
#: 跨过 ASR 流 reset 继续抵达；不吞掉就会被转写成"您的吗"之类的自说
#: 自话指令。窗口只需盖住 AEC 延迟 + 混响衰减，不追求覆盖整段播报。
_SETTLE_SECONDS = 0.6

#: ASR 文本必被状态机忽略的态（与 on_asr_text 里 _on_asr_ignored 的分支
#: 一致）。这些态喂流只会长回声文本，还会白耗识别算力。
_ASR_IGNORED_STATES = frozenset({State.SPEAKING, State.PROCESSING, State.EXECUTING})


class RouteDispatch:
    def __init__(self, *, streams, wakeword: str, spawn, settle_seconds: float = _SETTLE_SECONDS) -> None:
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
        self._last_text = ""
        self._settle_samples = int(round(settle_seconds * _SAMPLE_RATE))
        self._discard_until = 0
        self._tts_was_occupied = False
        self._wake_ring_start = 0
        #: 唤醒前缀标记：唤醒命中后置位，下一次 ASR 文本投递消费（SHUTDOWN 等
        #: 前缀门控分支区分裸说/带前缀投递；2026-10-06 计划 U2/U4）
        self._wake_prefix_pending = False
        #: 命令 KWS 待机闸状态（进待机 reset 一次，出待机恢复喂流）
        self._command_gate_active = False
        #: 唤醒前缀标记的采样窗上界：唤醒命中后 8 秒内的首条投递才算带前缀
        #: （ADV-03：标记不绑话语会被下一条任意来源投递偷走——唤醒后他人/电视
        #: 说出关机变体即无指令关机。8s 覆盖 replay + 自然接话节奏）
        self._wake_prefix_until = 0

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
        self._last_text = ""
        self._wake_pending = False
        self._discard_until = 0
        self._tts_was_occupied = False
        self._wake_prefix_pending = False
        self._command_gate_active = False
        for stream in (self._streams.asr_stream, self._streams.wake_stream,
                       self._streams.playback_stream,
                       getattr(self._streams, "command_stream", None)):
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
        self._last_text = ""
        self._wake_pending = False
        self._last_route = AudioRoute.LISTEN
        # 无 pre-roll 的进入（replay=False）都紧跟本机播报或本机提示语，
        # 同样开静默窗；唤醒进入则用户话音正连着 pre-roll，不能开窗。
        self._discard_until = (self._next_sample + self._settle_samples
                               if not replay else 0)
        # The event loop cannot interleave another feed with this synchronous snapshot.
        # 唤醒 replay 从唤醒词命中批次起放：命中之前的音频只可能是设备自
        # 说自话的尾音（真实指令都在唤醒词之后），回灌会再造回声。
        snapshot = ([seg for seg in self._ring if seg[0] >= self._wake_ring_start]
                    if replay else [])
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
        usable_text = text
        if usable_text.startswith(self._wakeword):
            usable_text = usable_text[len(self._wakeword):].lstrip(" ，,。.!！?？、")
        if usable_text and text != self._last_text:
            self._event("on_recognition_progress")
        self._last_text = text
        endpoint = stream.is_endpoint()
        # Segment long speech for bounded recognizer memory, without submitting an
        # unfinished user request. Only real silence allows delivery.
        if self._segment_samples >= 20 * 16000 or (endpoint and self._activity.active):
            if text:
                self._parts.append(text)
            stream.reset()
            self._segment_samples = 0
            self._last_text = ""
            return
        if endpoint and not self._activity.active:
            text = "".join(self._parts) + text
            self._parts.clear()
            stream.reset()
            self._segment_samples = 0
            self._last_text = ""
            if text.startswith(self._wakeword):
                text = text[len(self._wakeword):].lstrip(" ，,。.!！?？、")
            if text:
                VOICE_LOG.log("asr", f"识别到指令：{text}")
                self._deliver(text)
            else:
                self._event("on_empty_endpoint")

    def _deliver(self, text, *, wake_prefixed=None):
        """投递一句话文本。wake_prefixed=None（ASR 通道）时消费一次唤醒前缀
        标记——唤醒命中后的首条投递视为带前缀（「小丽拉立即去休息」）；
        KWS 通道（播放白名单/命令）显式传 False（命中词与唤醒无关，不得误耗标记）。
        """
        generation = self._generation
        if wake_prefixed is None:
            # ADV-03：前缀标记绑定唤醒后采样窗——窗外首条投递（他人/环境语音）
            # 不继承前缀，防无指令关机
            wake_prefixed = (self._wake_prefix_pending
                             and self._next_sample < self._wake_prefix_until)
            self._wake_prefix_pending = False
        async def deliver():
            if generation != self._generation:
                return
            engine = self._streams.engine
            if hasattr(engine, "listening_generation"):
                # U2 起引擎签名带 wake_prefixed（与 listening_generation 门同批判定）
                await engine.on_asr_text(text, generation=generation,
                                         wake_prefixed=wake_prefixed)
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
        occupied = bool(getattr(getattr(rt, "tts", None), "occupied", False))
        if self._tts_was_occupied and not occupied:
            # TTS 释放沿：AEC 清洗后的尾音尚未到齐，开窗吞弃。
            self._discard_until = max(self._discard_until,
                                      self._next_sample + self._settle_samples)
        self._tts_was_occupied = occupied
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
        # KWS stays active in every route: wake-word barge-in must survive
        # occupied TTS. Only the ASR dictation stream is gated below — device
        # self-audio (AEC residue) transcribed as user commands is the
        # "自说自话" failure mode, so feed ASR only while the device is
        # silent, the state actually consumes ASR text, and no settle window
        # is draining the post-playback echo tail.
        hit = rt.wake_stream.feed_and_poll(samples) if rt.wake_stream is not None else None
        if hit and self._wakeword in hit and not self._wake_pending:
            self._wake_pending = True
            self._wake_prefix_pending = True
            self._wake_prefix_until = start + 8 * _SAMPLE_RATE
            self._wake_ring_start = start
            VOICE_LOG.log("wake", f"收到语音唤醒（{self._wakeword}）")
            generation = self._generation
            async def wake():
                if generation == self._generation:
                    await rt.engine.on_wake()
            self._spawn(wake())
            return
        # 常驻命令 KWS（2026-10-06 计划 U2）：承载退出∪喊停高区分度声学子集——
        # 播报中喊停的唯一活通道（ASR 在 SPEAKING 被闸门关闭）。STANDBY（含勿扰
        # 待机——其退出走 ASR 文本通道）不喂不解码：命中结果一律被丢弃，白烧
        # 流式解码算力；进待机时 reset 丢弃跨墙解码态。唤醒命中块提前 return，
        # 同块命令投递让位唤醒（单块丢失，量级无害——计划已记）。
        command_stream = getattr(rt, "command_stream", None)
        if command_stream is not None:
            if getattr(rt.engine, "state", None) is State.STANDBY:
                if not self._command_gate_active:
                    command_stream.reset()
                    self._command_gate_active = True
            else:
                self._command_gate_active = False
                cmd_hit = command_stream.feed_and_poll(samples)
                if cmd_hit:
                    word = next((w for w in COMMAND_KWS_WORDS if w in cmd_hit), None)
                    if word is not None:
                        VOICE_LOG.log("command", f"命令通道命中：{word}")
                        self._deliver(word, wake_prefixed=False)
        if route is AudioRoute.LISTEN and not self._wake_pending:
            if (start < self._discard_until
                    or getattr(getattr(rt, "engine", None), "state", None) in _ASR_IGNORED_STATES
                    or occupied):
                # 只记账不喂流：fed_until 前移，防止唤醒 replay 把这段
                # （回声）音频再灌回识别流。
                self._fed_until = start + len(samples)
            else:
                self._feed_asr(start, samples)
        elif route is AudioRoute.PLAYBACK and rt.playback_stream is not None and not self._wake_pending:
            hit = rt.playback_stream.feed_and_poll(samples)
            if hit:
                word = hit.split("@")[-1].strip()
                VOICE_LOG.log("playback", f"播放控制语音：{word}")
                self._deliver(word, wake_prefixed=False)
