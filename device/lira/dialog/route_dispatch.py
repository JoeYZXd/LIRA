"""Continuous cleaned-audio dispatch, wake handoff and bounded in-memory pre-roll."""
from __future__ import annotations

from collections import deque
import logging
import numpy as np

from lira.audio.activity import SpeechActivity
from lira.dialog.callbacks import invoke_callback
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
        self._audio_samples = 0
        self._audio_energy = 0.0
        self._audio_peak = 0.0
        self._audio_clipped = 0
        self._asr_gate_reason = None
        self._identity_audio = deque(maxlen=100)
        #: utterance 单调计数（U3/KTD3）：每次真实投递分配；跨代次不复位，
        #: 与 turn 的映射由引擎在接受语句处建立
        self._utterance_id = 0

    @property
    def generation(self):
        return self._generation

    def clear(self) -> None:
        self._identity_audio.clear()
        self._generation += 1
        self._audio_samples = self._audio_clipped = 0
        self._audio_energy = self._audio_peak = 0.0
        self._asr_gate_reason = None
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
        VOICE_LOG.log("audio_gate", "隐私开启：停止向识别链路送音频" if enabled
                      else "隐私关闭：恢复识别链路输入")

    def begin_listening(self, *, replay: bool = True) -> None:
        self._identity_audio.clear()
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
        if getattr(self._streams, "speaker_identity", None) is not None and float(np.mean(samples*samples)) > 0.00001:
            self._identity_audio.append(samples.copy())
        self._segment_samples += len(samples)
        text = stream.text().strip()
        if text and text != self._last_text:
            VOICE_LOG.log("asr_partial", f"ASR 中间结果（未提交）：{text}")
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
                voice_samples=np.concatenate(list(self._identity_audio)) if self._identity_audio else None
                self._identity_audio.clear()
                self._deliver(text, voice_samples=voice_samples)
            else:
                VOICE_LOG.log("asr", "ASR 句末结果为空，未投递指令")
                self._event("on_empty_endpoint")

    def _next_utterance_id(self) -> int:
        self._utterance_id += 1
        return self._utterance_id

    def _emit_event(self, kind: str, text: str, **fields) -> None:
        """遥测旁路发射（U3）：未装配即 no-op；异常只留痕不进主链路。"""
        telemetry = getattr(self._streams, "telemetry", None)
        if telemetry is None:
            return
        try:
            telemetry.add_event(kind, text, **fields)
        except Exception:
            logging.debug("dispatch event=telemetry_emit_failed kind=%s", kind,
                          exc_info=True)

    def _deliver(self, text, *, wake_prefixed=None, voice_samples=None, source="asr"):
        """投递一句话文本。wake_prefixed=None（ASR 通道）时消费一次唤醒前缀
        标记——唤醒命中后的首条投递视为带前缀（「小丽拉立即去休息」）；
        KWS 通道（播放白名单/命令）显式传 False（命中词与唤醒无关，不得误耗标记）。

        source：投递来源标签（U3 utterance 事件文案前缀；ASR 句末不加前缀，
        命令/播放通道与既有 VOICE_LOG 文案同款区分度）。
        """
        generation = self._generation
        from_asr = wake_prefixed is None
        if wake_prefixed is None:
            # ADV-03：前缀标记绑定唤醒后采样窗——窗外首条投递（他人/环境语音）
            # 不继承前缀，防无指令关机
            wake_prefixed = (self._wake_prefix_pending
                             and self._next_sample < self._wake_prefix_until)
            self._wake_prefix_pending = False
        # U3/KTD3：真实投递即分配 utterance；事件在代次门通过后落盘——
        # 过期代次的投递被丢弃，天然无 turn（新轮次不接受旧事件）
        utterance_id = self._next_utterance_id()
        utterance_text = text if source == "asr" else f"{source}：{text}"
        async def deliver():
            if generation != self._generation:
                return
            self._emit_event("utterance", utterance_text,
                             capture_generation=generation,
                             utterance_id=utterance_id)
            engine = self._streams.engine
            handler=getattr(self._streams,"handle_voice_input",None)
            if from_asr and handler is not None and getattr(self._streams,"speaker_identity",None) is not None:
                await handler(text,voice_samples if voice_samples is not None else np.empty(0,np.float32),generation,wake_prefixed,utterance_id)
                return
            if hasattr(engine, "listening_generation"):
                # U2 起引擎签名带 wake_prefixed（与 listening_generation 门同批判定）；
                # utterance_id 经 invoke_callback 按签名过滤（无此参的旧替身不受影响）
                await invoke_callback(engine.on_asr_text, text, generation=generation,
                                      wake_prefixed=wake_prefixed,
                                      utterance_id=utterance_id)
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
        # 结构化遥测旁路（U2，KTD2）：隐私门之后的共享输入只读观察——与下方
        # 每秒 VOICE_LOG 统计同位，置于所有 early-return 之前，包络才反映真实
        # 设备输入。未装配（getattr None）或无观察者时遥测侧自会跳过计算。
        telemetry = getattr(rt, "telemetry", None)
        if telemetry is not None:
            telemetry.on_audio(samples, generation=self._generation,
                               route=route, tts_occupied=occupied)
        self._audio_samples += samples.size
        self._audio_energy += float(np.sum(samples * samples))
        if samples.size:
            self._audio_peak = max(self._audio_peak, float(np.max(np.abs(samples))))
            self._audio_clipped += int(np.count_nonzero(np.abs(samples) >= 0.999))
        if self._audio_samples >= _SAMPLE_RATE:
            rms = np.sqrt(self._audio_energy / self._audio_samples)
            db = 20 * np.log10(max(float(rms), 1e-10))
            VOICE_LOG.log("audio", f"收到处理后音频：RMS={db:.1f} dBFS，峰值={self._audio_peak:.3f}，"
                          f"削波={100 * self._audio_clipped / self._audio_samples:.1f}%，"
                          f"路由={route.value}，播报占用={occupied}；"
                          + ("唤醒 KWS 持续检测（未报告新命中）" if rt.wake_stream is not None
                             else "唤醒 KWS 未装配"))
            self._audio_samples = self._audio_clipped = 0
            self._audio_energy = self._audio_peak = 0.0
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
        if hit:
            reason = ("唤醒事件已排队，忽略重复命中" if self._wake_pending
                      else "匹配唤醒词，提交唤醒" if self._wakeword in hit
                      else "与配置唤醒词不匹配，不触发唤醒")
            VOICE_LOG.log("kws", f"KWS 结果：{hit}；目标={self._wakeword}；{reason}")
        if hit and self._wakeword in hit and not self._wake_pending:
            self._wake_pending = True
            self._wake_prefix_pending = True
            self._wake_prefix_until = start + 8 * _SAMPLE_RATE
            self._wake_ring_start = start
            VOICE_LOG.log("wake", f"收到语音唤醒（{self._wakeword}）")
            generation = self._generation
            async def wake():
                try:
                    if generation == self._generation:
                        await rt.engine.on_wake()
                        state = getattr(rt.engine, "state", None)
                        VOICE_LOG.log("wake", f"唤醒处理完成：状态={getattr(state, 'value', state)}")
                    else:
                        VOICE_LOG.log("wake", "唤醒事件已失效：音频会话代次改变")
                except Exception:
                    VOICE_LOG.log("wake", "唤醒处理失败：释放排队标记，可再次唤醒")
                    if generation == self._generation:
                        self._wake_prefix_pending = False
                    raise
                finally:
                    # begin_listening/clear may have started a successor. Never
                    # release that newer wake's pending flag from this task.
                    if generation == self._generation:
                        self._wake_pending = False
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
                        self._deliver(word, wake_prefixed=False, source="命令通道")
        if route is AudioRoute.LISTEN and not self._wake_pending:
            reason = ("播报占用" if occupied else "播报结束后的回声吞弃窗"
                      if start < self._discard_until else "当前状态不接收 ASR"
                      if getattr(rt.engine, "state", None) in _ASR_IGNORED_STATES else "正常识别")
            if reason != self._asr_gate_reason:
                VOICE_LOG.log("audio_gate", f"ASR 输入：{reason}")
                self._asr_gate_reason = reason
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
                self._deliver(word, wake_prefixed=False, source="播放控制")
