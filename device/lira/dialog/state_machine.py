"""对话状态机（U3）：LIRA 全部交互的汇点，纯逻辑、无 I/O（计划 U3 Approach）。

状态图（计划 High-Level Technical Design）::

    [*] → STANDBY ⇄ LISTENING → EXECUTING → {STANDBY | CONFIRMING | CAPTURING}
    CONFIRMING →(确认词)→ 发送 IR → STANDBY；→(拒绝/10s 超时/无关应答)→ STANDBY
    CAPTURING →(拍摄成功)→ READING ⇄(白名单命令)；→(停止/读完)→ STANDBY

设计要点：
  - 无 I/O：speak / send_ir / start_capture / start_listening / 播放控制 全部经
    `DialogCallbacks` 协议注入（x86 mock 可全面单测；main.py 装配真实 HAL）。
  - 音频路由由状态机统一切换（KWS / ASR / 白名单 KWS 三选二）：STANDBY=WAKE、
    会话各态=LISTEN、READING=PLAYBACK（R21 朗读期结构性隔离）。
  - 时间注入：`tick(now)` 虚拟时钟驱动 LISTENING 8s（R25）与 CONFIRMING 10s
    （R23 fail-closed）超时；测试注入任意时刻。
  - R20 会话模型：一次唤醒=一条指令；任务完成/取消即回 STANDBY，无跟随窗口；
    STANDBY 态收到的 ASR 文本一律忽略。
  - R11 意图隔离：READING 态只处理白名单精确命中的播放命令，其余文本一律忽略
    ——朗读内容在结构上进不了意图通道。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from lira.dialog import phrasebook as pb
from lira.dialog.intents import (
    Intent,
    IntentKind,
    classify_appliance_miss,
    classify_confirmation,
    find_alias_hits,
    is_cancel,
    is_playback_command,
)
from lira.dialog.router import Router, RouterTier

__all__ = ["State", "AudioRoute", "DialogCallbacks", "DialogEngine"]


class State(Enum):
    STANDBY = "standby"
    LISTENING = "listening"
    EXECUTING = "executing"
    CONFIRMING = "confirming"
    CAPTURING = "capturing"
    READING = "reading"


class AudioRoute(Enum):
    """麦克风分发路由（三选二）：WAKE=唤醒 KWS；LISTEN=流式 ASR；PLAYBACK=白名单 KWS。"""

    WAKE = "wake"
    LISTEN = "listen"
    PLAYBACK = "playback"


_ROUTE_BY_STATE: dict[State, AudioRoute] = {
    State.STANDBY: AudioRoute.WAKE,
    State.LISTENING: AudioRoute.LISTEN,
    State.EXECUTING: AudioRoute.LISTEN,
    State.CONFIRMING: AudioRoute.LISTEN,
    State.CAPTURING: AudioRoute.LISTEN,
    State.READING: AudioRoute.PLAYBACK,
}


class DialogCallbacks(Protocol):
    """状态机对外的全部副作用接口（main.py 装配；测试用 Recorder 注入）。"""

    def speak(self, text: str) -> None: ...

    def start_listening(self) -> None:
        """进入聆听时复位 ASR 流并开始收音。"""

    def set_audio_route(self, route: AudioRoute) -> None: ...

    def start_capture(self) -> None:
        """拍摄并进入 OCR 阅读流程（U4 接入点，R4 语音引导在 U4 内播报）。"""

    def send_ir(self, device: str, action: str) -> None: ...

    # R21/R22 播放控制（U4 阅读会话接入点）
    def playback_pause(self) -> None: ...

    def playback_resume(self) -> None: ...

    def playback_stop(self) -> None: ...

    def playback_read_again(self) -> None: ...

    def playback_volume_up(self) -> None: ...

    def playback_volume_down(self) -> None: ...


@dataclass
class _PendingConfirm:
    """CONFIRMING 态挂起的高危动作（fail-closed：确认前绝不发送）。"""

    device: str
    action: str

    @property
    def desc(self) -> str:
        return f"{self.action}{self.device}"


class DialogEngine:
    """对话状态机：迁移表 + 计时器 + 会话计数器，全部纯逻辑。"""

    #: R25 聆听窗口
    LISTEN_WINDOW_SECONDS = 8.0
    #: R25：超时复述提示一次后回待机（即最多 2 个窗口）
    LISTEN_MAX_TIMEOUTS = 2
    #: R23 高危确认窗口（超时不执行）
    CONFIRM_WINDOW_SECONDS = 10.0
    #: R24 同一会话识别失败引导最多 2 次，第 3 次礼貌回待机
    MAX_GUIDANCE_RETRIES = 2
    #: R26 拍摄失败引导重拍最多 2 次，仍失败告知请家人帮忙（U4 消费）
    MAX_CAPTURE_RETRIES = 2

    def __init__(
        self,
        router: Router,
        callbacks: DialogCallbacks,
        *,
        wake_allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        """Args:
        wake_allowed: 唤醒门谓词（U7 接入点）。隐私模式开启时装配层注入
            `lambda: not privacy.is_on`，隐私期 on_wake 一律忽略（不可唤醒，
            R12/R19）。默认恒真，不影响 U3 既有行为。
        """
        self._router = router
        self._cb = callbacks
        self._wake_allowed = wake_allowed
        self.now: float = 0.0
        self.state: State = State.STANDBY
        self.transitions: list[str] = []
        self._deadline: float | None = None
        self._listen_timeouts = 0
        self._guidance_count = 0
        self._restate_done = False
        self._capture_fails = 0
        self._pending: _PendingConfirm | None = None
        self._transition(State.STANDBY, initial=True)

    # ---------- 事件入口 ----------

    def tick(self, now: float) -> None:
        """虚拟时钟推进：处理 LISTENING/CONFIRMING 超时（编排器周期调用）。"""
        self.now = now
        if self._deadline is None:
            return
        if now < self._deadline:
            return
        if self.state is State.LISTENING:
            self._listen_timeouts += 1
            if self._listen_timeouts < self.LISTEN_MAX_TIMEOUTS:
                self._cb.speak(pb.LISTEN_REMIND)
                self._arm(self.LISTEN_WINDOW_SECONDS)
            else:
                self._cb.speak(pb.LISTEN_GOODBYE)
                self._enter_standby()
        elif self.state is State.CONFIRMING:
            # R23：10s 超时一律不执行（fail-closed）
            self._cb.speak(pb.CONFIRM_CANCELLED)
            self._enter_standby()

    async def on_wake(self) -> None:
        """主唤醒词命中（R16/R25）。仅 STANDBY 有效，会话中重复唤醒忽略（R20）。

        U7：隐私模式开启时一律忽略唤醒（不可唤醒），不留任何播报
        （麦克风已关，不存在可播报通道；恢复由物理按键 + 隐私关闭播报承担）。
        """
        if not self._wake_allowed():
            logging.info("唤醒事件被唤醒门拦截 event=wake_blocked reason=privacy")
            return
        if self.state is not State.STANDBY:
            logging.debug("会话中唤醒事件忽略 (state=%s)", self.state.value)
            return
        self._listen_timeouts = 0
        self._guidance_count = 0
        self._restate_done = False
        self._capture_fails = 0
        self._cb.speak(pb.WAKE_ACK)
        self._cb.start_listening()
        self._transition(State.LISTENING)
        self._arm(self.LISTEN_WINDOW_SECONDS)

    async def on_asr_text(self, text: str) -> None:
        """ASR 一句话文本（编排器在 endpoint 命中后调用）。按状态分发。"""
        t = text.strip()
        if not t:
            return
        handler = {
            State.STANDBY: self._on_asr_standby,
            State.LISTENING: self._on_asr_listening,
            State.CONFIRMING: self._on_asr_confirming,
            State.READING: self._on_asr_reading,
            State.EXECUTING: self._on_asr_ignored,
            State.CAPTURING: self._on_asr_capturing,
        }[self.state]
        await handler(t)

    async def on_capture_done(self, success: bool) -> None:
        """拍摄+OCR 流程回执（U4 注入）。成功 → READING；失败走 R26 引导。"""
        if self.state is not State.CAPTURING:
            return
        if success:
            self._capture_fails = 0
            self._transition(State.READING)
            return
        self._capture_fails += 1
        if self._capture_fails <= self.MAX_CAPTURE_RETRIES:
            self._cb.speak(pb.OCR_GUIDANCE)
            self._cb.start_capture()
        else:
            self._cb.speak(pb.OCR_GIVEUP)
            self._enter_standby()

    async def on_reading_finished(self) -> None:
        """朗读自然结束（全部块读完，R22）→ 回待机。"""
        if self.state is State.READING:
            self._enter_standby()

    # ---------- ASR 分发 ----------

    async def _on_asr_ignored(self, text: str) -> None:
        logging.debug("态 %s 忽略 ASR 文本", self.state.value)

    async def _on_asr_standby(self, text: str) -> None:
        # R20：无跟随窗口，待机态不响应任何未带唤醒词的语音
        logging.debug("STANDBY 忽略 ASR 文本（需先唤醒）")

    async def _on_asr_listening(self, text: str) -> None:
        if is_cancel(text):
            self._cb.speak(pb.SESSION_CANCELLED)
            self._enter_standby()
            return
        # G12（U6 接线）：家电别名命中多台 → 不猜、请求澄清（留在聆听态，可换说法）
        if len(find_alias_hits(text, self._router.appliances)) > 1:
            self._cb.speak(pb.APPLIANCE_AMBIGUOUS)
            self._arm(self.LISTEN_WINDOW_SECONDS)
            return
        # U6 接线：动作词命中但设备零别名 → 指向未配置设备，引导找家人在后台添加
        if classify_appliance_miss(text, self._router.appliances) == "unconfigured":
            self._cb.speak(pb.APPLIANCE_NOT_CONFIGURED)
            self._enter_standby()
            return
        result = await self._router.route(text)
        if result.tier is RouterTier.LOCAL_RULE and result.intent is not None:
            self._dispatch(result.intent)
        elif result.reply:
            # Tier 2/3 LLM 回复：播报即完成本条指令（R20）
            self._cb.speak(result.reply)
            self._enter_standby()
        else:
            # 规则与 LLM 均无结果 → R24 识别失败引导
            self._recognition_failure(text)

    async def _on_asr_confirming(self, text: str) -> None:
        assert self._pending is not None
        if is_cancel(text):
            self._cb.speak(pb.CONFIRM_CANCELLED)
            self._enter_standby()
            return
        verdict = classify_confirmation(text)
        if verdict == "confirm":
            pending, self._pending = self._pending, None
            self._cb.send_ir(pending.device, pending.action)
            self._cb.speak(pb.ir_sent_text(pending.desc))  # R29 措辞
            self._enter_standby()
        elif verdict == "reject":
            self._cb.speak(pb.CONFIRM_CANCELLED)
            self._enter_standby()
        else:
            # R23：语义不明 → 复述提示一次；再次不明 → 取消（fail-closed）
            if not self._restate_done:
                self._restate_done = True
                self._cb.speak(pb.CONFIRM_RESTATE)
                self._arm(self.CONFIRM_WINDOW_SECONDS)
            else:
                self._cb.speak(pb.CONFIRM_CANCELLED)
                self._enter_standby()

    async def _on_asr_reading(self, text: str) -> None:
        """READING 态：仅白名单精确命中进播放通道；其余一律忽略（R11/R21）。"""
        if is_cancel(text):
            self._cb.playback_stop()
            self._cb.speak(pb.SESSION_CANCELLED)
            self._enter_standby()
            return
        if not is_playback_command(text):
            logging.debug("READING 态忽略白名单外文本（朗读内容不进意图通道）")
            return
        playback_call = {
            "暂停": self._cb.playback_pause,
            "继续": self._cb.playback_resume,
            "停止": self._cb.playback_stop,
            "再读一遍": self._cb.playback_read_again,
            "大声点": self._cb.playback_volume_up,
            "小声点": self._cb.playback_volume_down,
        }[text]
        playback_call()
        if text == "停止":
            self._enter_standby()

    async def _on_asr_capturing(self, text: str) -> None:
        """拍摄中：仅响应全局取消（R20），其余忽略（拍摄引导由 U4 播报）。"""
        if is_cancel(text):
            self._cb.speak(pb.SESSION_CANCELLED)
            self._enter_standby()
            return
        logging.debug("CAPTURING 态忽略 ASR 文本")

    # ---------- 意图执行 ----------

    def _dispatch(self, intent: Intent) -> None:
        """本地规则意图执行（EXECUTING 为同步分发过程态）。"""
        self._transition(State.EXECUTING)
        if intent.kind is IntentKind.CANCEL:
            self._cb.speak(pb.SESSION_CANCELLED)
            self._enter_standby()
        elif intent.kind is IntentKind.HELP:
            self._cb.speak(pb.HELP_TEXT)
            self._enter_standby()
        elif intent.kind is IntentKind.READ:
            self._capture_fails = 0
            self._cb.start_capture()
            self._transition(State.CAPTURING)
            self._disarm()
        elif intent.kind is IntentKind.PLAYBACK:
            # 白名单命令只在 READING 态有效（见 _on_asr_reading），此处不应到达
            self._enter_standby()
        elif intent.kind is IntentKind.APPLIANCE:
            self._dispatch_appliance(intent)

    def _dispatch_appliance(self, intent: Intent) -> None:
        assert intent.appliance is not None and intent.action is not None
        appliance = intent.appliance
        if not appliance.enabled:
            # R7/AE3：被禁用设备拒绝执行并语音告知
            self._cb.speak(pb.DEVICE_DISABLED)
            self._enter_standby()
            return
        if appliance.is_high_risk:
            # R6/R23：高危加热设备必须二次确认（本地规则层，永不经 LLM）
            self._pending = _PendingConfirm(device=appliance.name, action=intent.action)
            self._cb.speak(pb.confirm_prompt_text(self._pending.desc))
            self._transition(State.CONFIRMING)
            self._arm(self.CONFIRM_WINDOW_SECONDS)
            return
        # 低危：直接发送（最后一道闸在 U6 send_ir 内部再查 enabled/high-risk）
        self._cb.send_ir(appliance.name, intent.action)
        self._cb.speak(pb.ir_sent_text(f"{intent.action}{appliance.name}"))
        self._enter_standby()

    # ---------- 内部机制 ----------

    def _recognition_failure(self, misheard: str) -> None:
        """R24：说出误解内容 + 示例引导；最多 2 次后礼貌回待机，绝不静默丢弃。"""
        self._guidance_count += 1
        if self._guidance_count <= self.MAX_GUIDANCE_RETRIES:
            self._cb.speak(pb.guidance_retry_text(misheard))
            self._cb.start_listening()
            self._arm(self.LISTEN_WINDOW_SECONDS)
        else:
            self._cb.speak(pb.GUIDANCE_GIVEUP)
            self._enter_standby()

    def _arm(self, window: float) -> None:
        self._deadline = self.now + window

    def _disarm(self) -> None:
        self._deadline = None

    def _transition(self, new: State, initial: bool = False) -> None:
        old = self.state
        self.state = new
        if not initial:
            self.transitions.append(f"{old.value}->{new.value}")
        self._cb.set_audio_route(_ROUTE_BY_STATE[new])

    def _enter_standby(self) -> None:
        self._pending = None
        self._disarm()
        self._listen_timeouts = 0
        self._restate_done = False
        self._transition(State.STANDBY)
