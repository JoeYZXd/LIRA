"""对话状态机（U3）：LIRA 全部交互的汇点，纯逻辑、无 I/O（计划 U3 Approach）。

状态图（计划 High-Level Technical Design）::

    [*] → STANDBY ⇄ LISTENING → PROCESSING → SPEAKING → FOLLOWUP
    FOLLOWUP → LISTENING；静默截止或结束聊天 → STANDBY
    CONFIRMING →(确认词)→ 发送 IR → STANDBY；→(拒绝/10s 超时/无关应答)→ STANDBY
    CAPTURING →(拍摄成功)→ READING ⇄(白名单命令)；→(停止/读完)→ STANDBY

设计要点：
  - 无 I/O：speak / send_ir / start_capture / start_listening / 播放控制 全部经
    `DialogCallbacks` 协议注入（x86 mock 可全面单测；main.py 装配真实 HAL）。
  - 主唤醒持续运行；状态仅决定 ASR 或受限阅读控制的输入权限。
  - 注入单调时钟：首次等待 8 秒，普通回答实际播完后追问 15 秒；有效讲话
    停止空闲倒计时，只有停滞输入或端点才触发恢复。
  - 会话和每次操作拥有独立代次；迟到识别、处理和播放结果不能影响后继。
  - R11 意图隔离：READING 态只处理白名单精确命中的播放命令，其余文本一律忽略
    ——朗读内容在结构上进不了意图通道。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from lira.dialog import phrasebook as pb
from lira.dialog.callbacks import invoke_callback as _invoke
from lira.dialog.context import ConversationHistory
from lira.dialog.intents import (
    Intent,
    IntentKind,
    classify_appliance_miss,
    classify_confirmation,
    find_alias_hits,
    is_cancel,
    is_exit_or_stop,
    is_book_fragment,
    match_local,
    is_playback_command,
)
from lira.dialog.router import Router, RouterTier
from lira.voice_log import VOICE_LOG

__all__ = ["State", "AudioRoute", "DialogCallbacks", "DialogEngine", "OperationOwner"]


class State(Enum):
    STANDBY = "standby"
    LISTENING = "listening"
    EXECUTING = "executing"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    FOLLOWUP = "followup"
    CLARIFYING = "clarifying"
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
    State.PROCESSING: AudioRoute.LISTEN,
    State.SPEAKING: AudioRoute.LISTEN,
    State.FOLLOWUP: AudioRoute.LISTEN,
    State.CLARIFYING: AudioRoute.LISTEN,
    State.CONFIRMING: AudioRoute.LISTEN,
    State.CAPTURING: AudioRoute.LISTEN,
    State.READING: AudioRoute.PLAYBACK,
}


class DialogCallbacks(Protocol):
    """状态机对外的全部副作用接口（main.py 装配；测试用 Recorder 注入）。"""

    def speak(self, text: str, *, owner=None) -> object: ...

    def chime(self, *, owner=None) -> object: ...

    def cancel_audio(self, owner) -> None: ...

    async def stop_audio(self, owner) -> None: ...

    def input_generation(self) -> int: ...

    def start_listening(self, *, replay: bool = True) -> None:
        """进入聆听时复位 ASR 流并开始收音。"""

    def set_audio_route(self, route: AudioRoute) -> None: ...

    def start_capture(self, *, owner=None) -> object:
        """拍摄并进入 OCR 阅读流程（U4 接入点，R4 语音引导在 U4 内播报）。

        返回值：在途拍摄任务句柄（Task 形，含 cancel()）或 None——装配层若以
        ensure_future 启动 `run_capture`，应把任务句柄返回给状态机；取消离开
        CAPTURING 时由状态机中止它（F1：否则在途拍摄继续读图并自行开播朗读）。
        """

    def send_ir(self, device: str, action: str, *, owner=None, confirmed=False) -> object: ...

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
    owner: object = None

    @property
    def desc(self) -> str:
        return f"{self.action}{self.device}"


@dataclass(frozen=True)
class OperationOwner:
    epoch: int
    operation_id: int


class DialogEngine:
    """对话状态机：迁移表 + 计时器 + 会话计数器，全部纯逻辑。"""

    #: R25 聆听窗口
    LISTEN_WINDOW_SECONDS = 8.0
    FOLLOWUP_WINDOW_SECONDS = 15.0
    INPUT_STALL_SECONDS = 30.0
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
            R12）。默认恒真，不影响 U3 既有行为。
        """
        self._router = router
        self._cb = callbacks
        self._wake_allowed = wake_allowed
        self.now: float = 0.0
        self.state: State = State.STANDBY
        self.transitions: list[str] = []
        self._deadline: float | None = None
        self._idle_deadline: float | None = None
        self.read_original = False
        self._guidance_count = 0
        self._restate_done = False
        self._capture_fails = 0
        self._pending: _PendingConfirm | None = None
        self._pending_reading: OperationOwner | None = None
        self._history = ConversationHistory()
        self._request_text = ""
        #: 在途拍摄任务句柄（start_capture 回调返回值；F1 取消中止用）
        self._capture_task: object | None = None
        self._epoch = 0
        self._operation_id = 0
        self._owner: OperationOwner | None = None
        self._route_task: asyncio.Task | None = None
        self._completion_tasks: set[asyncio.Task] = set()
        self._listening_generation = 0
        self._speech_active = False
        self._awaiting_endpoint = False
        self._last_audio_progress = 0.0
        self._last_speech_progress = 0.0
        self._last_recognition_progress = 0.0
        self._confirmed_ir: tuple[OperationOwner, str, str] | None = None
        self._effect_tasks: set[asyncio.Task] = set()
        self._last_completion = None
        self._transition(State.STANDBY, initial=True)

    # ---------- 事件入口 ----------

    def tick(self, now: float) -> None:
        """虚拟时钟推进：处理 LISTENING/CONFIRMING 超时（编排器周期调用）。"""
        self.now = now
        if self.state in (State.LISTENING, State.FOLLOWUP, State.CLARIFYING, State.CONFIRMING) and self._awaiting_endpoint:
            stalled = now - self._last_audio_progress >= self.INPUT_STALL_SECONDS
            endpoint_stalled = (not self._speech_active and
                                now - self._last_speech_progress >= self.INPUT_STALL_SECONDS)
            recognition_stalled = now - self._last_recognition_progress >= self.INPUT_STALL_SECONDS
            if stalled or endpoint_stalled or recognition_stalled:
                self._speech_active = self._awaiting_endpoint = False
                self._speak(pb.INPUT_NOT_HEARD)
                if self.state is State.CONFIRMING:
                    self._finish_command()
                    return
                self._begin_listening(replay=False)
                self._arm(self.LISTEN_WINDOW_SECONDS)
            return
        if self._deadline is None:
            return
        if now < self._deadline:
            return
        if self.state in (State.LISTENING, State.FOLLOWUP, State.CLARIFYING):
            self._enter_standby()
        elif self.state is State.CONFIRMING:
            # R23：10s 超时一律不执行（fail-closed）
            self._speak(pb.CONFIRM_CANCELLED)
            self._finish_command()

    async def on_wake(self) -> None:
        """任何非隐私状态接受唤醒，停止旧输出后接收连续说出的指令。

        U7：隐私模式开启时一律忽略唤醒（不可唤醒），不留任何播报
        （麦克风已关，不存在可播报通道；恢复由隐私关闭播报承担）。
        """
        if not self._wake_allowed():
            logging.info("唤醒事件被唤醒门拦截 event=wake_blocked reason=privacy")
            return
        if self.state in (State.READING, State.CAPTURING):
            self._cb.playback_pause()
        old_owner = self._owner
        owner = self._new_operation(new_session=self.state is State.STANDBY)
        stop = getattr(self._cb, "stop_audio", None)
        if stop is not None:
            try:
                await stop(old_owner)
            except Exception:
                logging.exception("dialog event=playback_stop_failed")
                if self.is_current(owner):
                    self._begin_listening(replay=True)
                    self._speak(pb.PLAYBACK_FAILED)
                    self._arm(self.LISTEN_WINDOW_SECONDS)
                return
        if not self.is_current(owner):
            return
        self.read_original = False
        self._guidance_count = 0
        self._restate_done = False
        self._capture_fails = 0
        self._begin_listening(replay=True)
        chime = getattr(self._cb, "chime", None)
        if chime is not None:
            _invoke(chime, owner=owner)
        else:
            self._speak(pb.WAKE_ACK)
        self._arm(self.LISTEN_WINDOW_SECONDS)

    @property
    def owner(self) -> OperationOwner | None:
        return self._owner

    @property
    def listening_generation(self) -> int:
        return self._listening_generation

    def is_current(self, owner: object) -> bool:
        return owner is not None and owner == self._owner and self._wake_allowed()

    def admit_ir(self, owner: object, device: str, action: str, *, confirmed: bool) -> bool:
        if not self.is_current(owner):
            return False
        return not confirmed or self._confirmed_ir == (owner, device, action)

    def _input_current(self, generation: int | None) -> bool:
        if generation is None:
            getter = getattr(self._cb, "input_generation", None)
            generation = getter() if getter is not None else self._listening_generation
        return generation == self._listening_generation and self._wake_allowed()

    def on_speech_started(self, generation: int) -> None:
        if self._input_current(generation) and self.state in (State.LISTENING, State.FOLLOWUP, State.CLARIFYING, State.CONFIRMING):
            self._speech_active = self._awaiting_endpoint = True
            self._last_audio_progress = self._last_speech_progress = self.now
            self._last_recognition_progress = self.now
            self._deadline = None
            if self.state is State.FOLLOWUP:
                self._transition(State.LISTENING)

    def on_speech_ended(self, generation: int) -> None:
        if self._input_current(generation) and self._awaiting_endpoint:
            self._speech_active = False
            self._last_speech_progress = self.now

    def on_empty_endpoint(self, generation: int) -> None:
        """Wake-only/noise endpoints must not extend the original idle window."""
        if self._input_current(generation) and self.state in (State.LISTENING, State.FOLLOWUP, State.CLARIFYING, State.CONFIRMING):
            self._speech_active = self._awaiting_endpoint = False
            self._deadline = self._idle_deadline

    def on_audio_progress(self, generation: int) -> None:
        if self._input_current(generation):
            self._last_audio_progress = self.now
            if self._speech_active:
                self._last_speech_progress = self.now

    def on_recognition_progress(self, generation: int) -> None:
        if self._input_current(generation) and self._awaiting_endpoint:
            self._last_recognition_progress = self.now

    async def on_input_error(self) -> None:
        if self.state in (State.LISTENING, State.FOLLOWUP, State.CLARIFYING, State.CONFIRMING):
            self._speak(pb.INPUT_NOT_HEARD)
            self._begin_listening(replay=False)
            self._arm(self.LISTEN_WINDOW_SECONDS)

    def apply_privacy(self, enabled: bool) -> None:
        if enabled:
            self._cb.playback_pause()
            self._enter_standby()

    def close(self) -> None:
        self._enter_standby()

    async def on_asr_text(self, text: str, *, generation: int | None = None,
                          wake_prefixed: bool = False) -> None:
        """ASR 一句话文本（编排器在 endpoint 命中后调用）。按状态分发。

        wake_prefixed：投递前是否命中过唤醒词（route_dispatch 前缀标记）。
        U4 真关机分支仅在前缀投递时提交（origin R4）；其余分支忽略该标记。
        """
        t = text.strip()
        if not t or not self._input_current(generation):
            return
        # R1/R6 态前统一退出分支：取消词 + 结束聊天 + 统一退出∪喊停词集
        # （含「关机」——R5 语义为结束会话，永不触发真关机）。任何非待机态命中
        # 即停播/停动作回待机，不等超时；待机不监听（R3）。CONFIRMING 中命中
        # 与「取消」同路径：静默回待机、挂起动作必然不执行（fail-closed 出口
        # 统一，不另设话术分支）。READING 中退出/喊停词 → 停止（非暂停），
        # 「结束聊天」保留既有暂停语义。
        if self.state is not State.STANDBY and (
            is_cancel(t) or t == "结束聊天" or is_exit_or_stop(t)
        ):
            if self.state is State.READING:
                if t == "结束聊天":
                    self._cb.playback_pause()
                else:
                    self._cb.playback_stop()
            self._enter_standby()
            return
        self._speech_active = self._awaiting_endpoint = False
        handler = {
            State.STANDBY: self._on_asr_standby,
            State.LISTENING: self._on_asr_listening,
            State.FOLLOWUP: self._on_asr_listening,
            State.PROCESSING: self._on_asr_ignored,
            State.SPEAKING: self._on_asr_ignored,
            State.CLARIFYING: self._on_asr_clarifying,
            State.CONFIRMING: self._on_asr_confirming,
            State.READING: self._on_asr_reading,
            State.EXECUTING: self._on_asr_ignored,
            State.CAPTURING: self._on_asr_capturing,
        }[self.state]
        await handler(t)

    async def on_capture_done(self, success: bool, *, owner: object = None) -> None:
        """拍摄+OCR 流程回执（U4 注入）。成功 → READING；失败走 R26 引导。"""
        if self.state is not State.CAPTURING or (owner is not None and not self.is_current(owner)):
            return
        if success:
            self._capture_fails = 0
            self._capture_task = None  # 已完成，句柄作废
            self._transition(State.READING)
            return
        facts = getattr(self._cb, "reading_task_facts", lambda: None)()
        if facts is not None and facts.resumable:
            # OCR succeeded and cached its text; a cue failure must not recapture
            # a page the user may already have removed from the camera.
            self._capture_task = None
            self._speak_answer("已经识别并保存了内容，但播报失败。请说继续来重试。",
                               owner=self._owner, user_text=self._request_text)
            return
        self._capture_fails += 1
        if self._capture_fails <= self.MAX_CAPTURE_RETRIES:
            self._speak(pb.OCR_GUIDANCE)
            self._capture_task = _invoke(self._cb.start_capture, owner=self._owner)
        else:
            self._speak(pb.OCR_GIVEUP)
            self._finish_command()

    async def on_reading_finished(self, *, owner: object = None) -> None:
        """朗读自然结束（全部块读完，R22）→ 回待机。"""
        if self.state is State.READING and (owner is None or self.is_current(owner)):
            self._enter_standby()

    # ---------- ASR 分发 ----------

    async def _on_asr_ignored(self, text: str) -> None:
        logging.debug("态 %s 忽略 ASR 文本", self.state.value)

    async def _on_asr_standby(self, text: str) -> None:
        # 会话已关闭；待机态不响应未带唤醒词的语音。
        logging.debug("STANDBY 忽略 ASR 文本（需先唤醒）")

    async def _on_asr_listening(self, text: str) -> None:
        if is_cancel(text):
            self._speak(pb.SESSION_CANCELLED)
            self._finish_command()
            return
        if is_book_fragment(text):
            self._start_reading_clarification()
            return
        # G12（U6 接线）：家电别名命中多台 → 不猜、请求澄清（留在聆听态，可换说法）
        if len(find_alias_hits(text, self._router.appliances)) > 1:
            self._speak(pb.APPLIANCE_AMBIGUOUS)
            self._arm(self.LISTEN_WINDOW_SECONDS)
            return
        # U6 接线：动作词命中但设备零别名 → 指向未配置设备，引导找家人在后台添加
        if classify_appliance_miss(text, self._router.appliances) == "unconfigured":
            self._speak(pb.APPLIANCE_NOT_CONFIGURED)
            self._finish_command()
            return
        owner = self._new_operation()
        self._request_text = text
        facts = getattr(self._cb, "reading_task_facts", lambda: None)()
        context = self._history.snapshot(task=facts)
        self._transition(State.PROCESSING)
        self._disarm()
        self._route_task = asyncio.current_task()
        try:
            result = await self._router.route(
                text, context=context, on_wait_feedback=lambda: self.speak_progress(owner, pb.WAIT_REMOTE))
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("dialog event=understanding_failed")
            if self.is_current(owner):
                self._speak(pb.UNDERSTANDING_FAILED)
                self._begin_listening(replay=False)
                self._arm(self.LISTEN_WINDOW_SECONDS)
            return
        finally:
            if self.is_current(owner):
                self._route_task = None
        if not self.is_current(owner):
            return
        if result.tier is RouterTier.LOCAL_RULE and result.intent is not None:
            self._dispatch(result.intent)
        elif result.reply:
            self._speak_answer(result.reply, owner=owner, user_text=text)
        else:
            # 规则与 LLM 均无结果 → R24 识别失败引导
            self._recognition_failure(text)

    def _start_reading_clarification(self) -> None:
        owner = self._new_operation()
        self._begin_listening(replay=False)
        self._pending_reading = owner
        self._transition(State.CLARIFYING)
        self._disarm()
        completion = self._speak("您是要我读这本书吗？")
        def finished():
            if not self.is_current(owner) or self.state is not State.CLARIFYING:
                return
            if completion is not None and getattr(completion, "status", "success") != "success":
                self._enter_standby()
                return
            self._idle_deadline = self.now + self.FOLLOWUP_WINDOW_SECONDS
            if not self._awaiting_endpoint:
                self._deadline = self._idle_deadline
        if completion is None or completion.is_set():
            finished()
        else:
            async def wait():
                await completion.wait()
                finished()
            self._track_completion(wait())

    async def _on_asr_clarifying(self, text: str) -> None:
        if self._pending_reading != self._owner:
            self._enter_standby()
            return
        answer = text.strip().rstrip("。！？!?，,")
        verdict = ("confirm" if answer in ("是", "是的", "对", "好", "好的", "读吧", "确认") else
                   "reject" if answer in ("不", "不是", "不要", "不对", "不用", "不是的") else None)
        if verdict == "confirm":
            self._new_operation()
            self._dispatch(Intent(kind=IntentKind.READ, text="读这本书"))
        elif verdict == "reject":
            self._new_operation()
            self._begin_listening(replay=False)
            self._arm(self.LISTEN_WINDOW_SECONDS)
        elif match_local(text, self._router.appliances) is not None:
            await self._on_asr_listening(text)
        else:
            self._start_reading_clarification()

    async def _on_asr_confirming(self, text: str) -> None:
        assert self._pending is not None
        if self._pending.owner != self._owner:
            self._enter_standby()
            return
        if is_cancel(text):
            self._speak(pb.CONFIRM_CANCELLED)
            self._finish_command()
            return
        verdict = classify_confirmation(text)
        if verdict == "confirm":
            pending, self._pending = self._pending, None
            owner = self._owner
            self._confirmed_ir = (owner, pending.device, pending.action)
            self._send_ir(pending.device, pending.action, confirmed=True)
            self._speak(pb.ir_sent_text(pending.desc))  # R29 措辞
            self._finish_command()
        elif verdict == "reject":
            self._speak(pb.CONFIRM_CANCELLED)
            self._finish_command()
        else:
            # R23：语义不明 → 复述提示一次；再次不明 → 取消（fail-closed）
            if not self._restate_done:
                self._restate_done = True
                self._speak(pb.CONFIRM_RESTATE)
                self._arm(self.CONFIRM_WINDOW_SECONDS)
            else:
                self._speak(pb.CONFIRM_CANCELLED)
                self._finish_command()

    async def _on_asr_reading(self, text: str) -> None:
        """READING 态：仅白名单精确命中进播放通道；其余一律忽略（R11/R21）。"""
        if is_cancel(text):
            self._cb.playback_stop()
            self._speak(pb.SESSION_CANCELLED)
            self._finish_command()
            return
        if not is_playback_command(text):
            logging.debug("READING 态忽略白名单外文本（朗读内容不进意图通道）")
            return
        self._playback_control(text)
        if text == "停止":
            self._enter_standby()

    async def _on_asr_capturing(self, text: str) -> None:
        """拍摄中：仅响应全局取消（R20），其余忽略（拍摄引导由 U4 播报）。"""
        if is_cancel(text):
            self._speak(pb.SESSION_CANCELLED)
            self._finish_command()
            return
        logging.debug("CAPTURING 态忽略 ASR 文本")

    # ---------- 意图执行 ----------

    def _dispatch(self, intent: Intent) -> None:
        """本地规则意图执行（EXECUTING 为同步分发过程态）。"""
        self._transition(State.EXECUTING)
        if intent.kind is IntentKind.CANCEL:
            self._speak(pb.SESSION_CANCELLED)
            self._finish_command()
        elif intent.kind is IntentKind.HELP:
            self._speak_answer(pb.HELP_TEXT, owner=self._owner, user_text=self._request_text)
        elif intent.kind is IntentKind.READ:
            self._capture_fails = 0
            # 读书用于边听边核对，整次会话（含重拍）保留原文模式。
            self.read_original = "书" in intent.text
            self._transition(State.CAPTURING)
            self._disarm()
            self._capture_task = _invoke(self._cb.start_capture, owner=self._owner)
        elif intent.kind is IntentKind.PLAYBACK:
            facts = getattr(self._cb, "reading_task_facts", lambda: None)()
            if facts is None or not facts.resumable:
                self._speak_answer("没有可以继续的阅读内容，请先让我读书。", owner=self._owner,
                                   user_text=self._request_text)
                return
            self._playback_control(intent.text)
            if intent.text in ("继续", "再读一遍"):
                self._disarm()
                self._transition(State.READING)
            elif intent.text == "停止":
                self._enter_standby()
            else:
                self._begin_listening(replay=False)
                self._arm(self.FOLLOWUP_WINDOW_SECONDS)
        elif intent.kind is IntentKind.APPLIANCE:
            self._dispatch_appliance(intent)

    def _dispatch_appliance(self, intent: Intent) -> None:
        assert intent.appliance is not None and intent.action is not None
        appliance = intent.appliance
        if not appliance.enabled:
            # R7/AE3：被禁用设备拒绝执行并语音告知
            self._speak(pb.DEVICE_DISABLED)
            self._finish_command()
            return
        if appliance.is_high_risk:
            # R6/R23：高危加热设备必须二次确认（本地规则层，永不经 LLM）
            self._pending = _PendingConfirm(device=appliance.name, action=intent.action, owner=self._owner)
            self._speak(pb.confirm_prompt_text(self._pending.desc))
            self._transition(State.CONFIRMING)
            self._arm(self.CONFIRM_WINDOW_SECONDS)
            return
        # 低危：直接发送（最后一道闸在 U6 send_ir 内部再查 enabled/high-risk）
        self._send_ir(appliance.name, intent.action, confirmed=False)
        self._speak(pb.ir_sent_text(f"{intent.action}{appliance.name}"))
        self._finish_command()

    # ---------- 内部机制 ----------

    def _playback_control(self, text: str) -> None:
        controls = {
            "暂停": self._cb.playback_pause, "继续": self._cb.playback_resume,
            "停止": self._cb.playback_stop, "再读一遍": self._cb.playback_read_again,
            "大声点": self._cb.playback_volume_up, "小声点": self._cb.playback_volume_down,
        }
        controls[text]()

    def _recognition_failure(self, misheard: str) -> None:
        """R24：说出误解内容 + 示例引导；最多 2 次后礼貌回待机，绝不静默丢弃。"""
        self._guidance_count += 1
        if self._guidance_count <= self.MAX_GUIDANCE_RETRIES:
            self._speak(pb.guidance_retry_text(misheard))
            self._begin_listening(replay=False)
            self._arm(self.LISTEN_WINDOW_SECONDS)
        else:
            self._speak(pb.GUIDANCE_GIVEUP)
            self._finish_command()

    def _arm(self, window: float) -> None:
        self._deadline = self.now + window
        self._idle_deadline = self._deadline

    def _disarm(self) -> None:
        self._deadline = None
        self._idle_deadline = None

    def _transition(self, new: State, initial: bool = False) -> None:
        old = self.state
        self.state = new
        if not initial:
            self.transitions.append(f"{old.value}->{new.value}")
        self._cb.set_audio_route(_ROUTE_BY_STATE[new])
        _voice_log_state(new, initial=initial)

    def _enter_standby(self) -> None:
        self._invalidate_operation()
        self._epoch += 1
        self._history.clear()
        self._pending_reading = None
        self._pending = None
        self._confirmed_ir = None
        self._speech_active = self._awaiting_endpoint = False
        self._disarm()
        self.read_original = False
        self._restate_done = False
        self._transition(State.STANDBY)

    def _finish_command(self) -> None:
        """Close only after the owned feedback and admitted effect finish."""
        self._pending = None
        self._disarm()
        self._speech_active = self._awaiting_endpoint = False
        owner, completion = self._owner, self._last_completion
        effects = tuple(self._effect_tasks)
        if ((completion is None or completion.is_set()) and
                all(task.done() for task in effects)):
            self._enter_standby()
            return
        self._transition(State.SPEAKING)
        async def finish():
            if completion is not None:
                await completion.wait()
            if effects:
                await asyncio.gather(*effects, return_exceptions=True)
            if self.is_current(owner):
                self._enter_standby()
        self._track_completion(finish())

    def _send_ir(self, device: str, action: str, *, confirmed: bool) -> None:
        task = _invoke(self._cb.send_ir, device, action, owner=self._owner, confirmed=confirmed)
        if isinstance(task, asyncio.Task):
            self._effect_tasks.add(task)
            task.add_done_callback(self._effect_tasks.discard)

    def _invalidate_operation(self) -> None:
        old, self._owner = self._owner, None
        self._pending_reading = None
        self._pending = None
        self._restate_done = False
        self._abort_capture()
        task, self._route_task = self._route_task, None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not None and task is not current and not task.done():
            task.cancel()
        cancel = getattr(self._cb, "cancel_audio", None)
        if old is not None and cancel is not None:
            cancel(old)
        for task in tuple(self._completion_tasks):
            if task is not current:
                task.cancel()
        self._completion_tasks.clear()
        for task in tuple(self._effect_tasks):
            if task is not current:
                task.cancel()
        self._effect_tasks.clear()
        self._last_completion = None
        self._confirmed_ir = None

    def _new_operation(self, *, new_session=False) -> OperationOwner:
        self._invalidate_operation()
        if new_session:
            self._history.clear()
            self._epoch += 1
        self._operation_id += 1
        self._owner = OperationOwner(self._epoch, self._operation_id)
        return self._owner

    def _begin_listening(self, *, replay: bool) -> None:
        self._speech_active = self._awaiting_endpoint = False
        getter = getattr(self._cb, "input_generation", None)
        self._listening_generation = (getter() if getter is not None else self._listening_generation) + 1
        self._transition(State.LISTENING)
        # Pre-roll can synchronously emit onset; bind its generation first.
        _invoke(self._cb.start_listening, replay=replay)
        self._last_audio_progress = self._last_speech_progress = self.now

    def _speak(self, text: str):
        self._last_completion = _invoke(self._cb.speak, text, owner=self._owner)
        return self._last_completion

    def speak_progress(self, owner: object, text: str) -> None:
        if self.is_current(owner) and self.state in (State.PROCESSING, State.CAPTURING):
            self._speak(text)

    def _track_completion(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._completion_tasks.add(task)
        task.add_done_callback(self._completion_tasks.discard)

    def _speak_answer(self, text: str, *, owner: OperationOwner, user_text: str) -> None:
        self._transition(State.SPEAKING)
        self._disarm()
        completion = self._speak(text)
        def finished():
            if not self.is_current(owner) or self.state is not State.SPEAKING:
                return
            if completion is None or getattr(completion, "status", "success") == "success":
                self._history.record_completed(user_text, text)
                self._begin_listening(replay=False)
                self._transition(State.FOLLOWUP)
                self._arm(self.FOLLOWUP_WINDOW_SECONDS)
            else:
                self._speak(pb.PLAYBACK_FAILED)
                self._begin_listening(replay=False)
                self._arm(self.LISTEN_WINDOW_SECONDS)
        if completion is None or completion.is_set():
            finished()
            return
        async def wait():
            await completion.wait()
            finished()
        self._track_completion(wait())

    def _abort_capture(self) -> None:
        """F1：离开 CAPTURING 时中止在途拍摄任务。

        不取消则 `run_capture` 继续拍图/OCR 并自行开播朗读会话——状态机虽已
        回待机（迟到的 on_capture_done 被 `on_capture_done` 的态守卫丢弃），
        朗读却照常出声。取消已完成/无句柄的任务是安全的 no-op。
        """
        task, self._capture_task = self._capture_task, None
        if task is not None and hasattr(task, "cancel"):
            task.cancel()


#: 状态 -> 语音交互日志文案（dev 展示面；STANDBY 文案按来源二分，见 _voice_log_state）
_STATE_VOICE_LABELS = {
    State.LISTENING: "聆听：等待语音指令",
    State.EXECUTING: "指令执行中",
    State.PROCESSING: "正在理解指令",
    State.SPEAKING: "回答播放中",
    State.FOLLOWUP: "等待本轮追问",
    State.CLARIFYING: "等待确认阅读这本书",
    State.CONFIRMING: "高危设备：等待语音确认",
    State.CAPTURING: "拍摄取书中",
    State.READING: "朗读中",
}


def _voice_log_state(new: State, *, initial: bool) -> None:
    """状态流转写入语音交互日志（lira.voice_log：内存展示区，不落盘）。"""
    if new is State.STANDBY:
        label = (
            "待机：持续等待唤醒中"
            if initial
            else "退出唤醒状态，回到待机，等待下次唤醒"
        )
    else:
        label = _STATE_VOICE_LABELS[new]
    VOICE_LOG.log("state", label)
