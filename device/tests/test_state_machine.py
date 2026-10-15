"""U3 对话状态机测试（迁移表，计划 Test scenarios 8 条 + 补充缺口）。

覆盖：AE3 高危确认 happy/error/超时（fail-closed）、AE4 READING 白名单隔离、
R20 会话模型（一次唤醒=一条指令）、R24 识别失败引导（≤2 次后礼貌回待机）、
R25 聆听窗口（8s 超时复述一次）、R31 帮助、任意态"取消/算了"、
CaptureGoal→READING 拍摄流程占位（R22/R26 接口）、会话内 LLM 降级。

纯逻辑状态机：无 I/O，动作经 Recorder 回调注入；时钟经 tick(now) 注入（虚拟时钟）。
"""

from __future__ import annotations

import asyncio

import pytest

from lira.dialog import (
    AudioRoute,
    DialogEngine,
    Router,
    State,
    phrasebook as pb,
)
from lira.dialog.intents import Appliance

T0 = 1000.0

APPLIANCES = (
    Appliance(
        name="取暖器",
        aliases=("取暖器",),
        actions={"打开": ("打开",), "关闭": ("关闭",)},
        is_high_risk=True,
    ),
    Appliance(
        name="台灯",
        aliases=("台灯",),
        actions={"打开": ("打开",), "关闭": ("关闭",)},
        is_high_risk=False,
    ),
    Appliance(
        name="加湿器",
        aliases=("加湿器",),
        actions={"打开": ("打开",)},
        is_high_risk=False,
        enabled=False,
    ),
)


class Recorder:
    """纯内存回调记录器：断言状态机的全部副作用。"""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.ir: list[tuple[str, str]] = []
        self.routes: list[AudioRoute] = []
        self.captures = 0
        self.playback: list[str] = []
        self.listening_starts = 0
        self.poweroffs = 0
        self.volume_deltas: list[float] = []

    # DialogCallbacks 协议
    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def start_listening(self) -> None:
        self.listening_starts += 1

    def set_audio_route(self, route: AudioRoute) -> None:
        self.routes.append(route)

    def start_capture(self) -> None:
        self.captures += 1

    def send_ir(self, device: str, action: str) -> None:
        self.ir.append((device, action))

    def playback_pause(self) -> None:
        self.playback.append("pause")

    def playback_resume(self) -> None:
        self.playback.append("resume")

    def playback_stop(self) -> None:
        self.playback.append("stop")

    def playback_read_again(self) -> None:
        self.playback.append("read_again")

    def playback_volume_up(self) -> None:
        self.playback.append("volume_up")

    def playback_volume_down(self) -> None:
        self.playback.append("volume_down")

    def poweroff(self) -> None:
        self.poweroffs += 1

    def adjust_volume(self, delta: float) -> None:
        self.volume_deltas.append(delta)

    def status_facts(self):
        return {}


def build_engine(rec: Recorder, **router_kwargs) -> DialogEngine:
    router = Router(appliances=APPLIANCES, **router_kwargs)
    return DialogEngine(router=router, callbacks=rec)


async def enter_state(sm: DialogEngine, rec: Recorder, target: State) -> None:
    """驱动状态机进入目标态（迁移表测试辅助）。"""
    sm.tick(T0)
    await sm.on_wake()
    assert sm.state is State.LISTENING
    if target is State.LISTENING:
        return
    if target is State.CONFIRMING:
        await sm.on_asr_text("打开取暖器")
    elif target is State.CAPTURING:
        await sm.on_asr_text("帮我读一下这个")
    elif target is State.READING:
        await sm.on_asr_text("帮我读一下这个")
        await sm.on_capture_done(True)
    assert sm.state is target


# ---------- Covers AE3. 高危确认（R23 fail-closed） ----------


class TestAe3HighRiskConfirm:
    async def test_confirm_happy_path_sends_ir(self):
        """Test scenario 1: 高危指令 → CONFIRMING → "确认" → send_ir 参数正确。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CONFIRMING)

        assert rec.ir == [], "确认前不得发射红外"
        assert any("确认" in s for s in rec.spoken), "应有二次确认提示"

        await sm.on_asr_text("确认")
        assert rec.ir == [("取暖器", "打开")]
        assert sm.state is State.STANDBY
        # R29 措辞：只能陈述"指令已发出"
        assert any("已发出" in s for s in rec.spoken)

    async def test_reject_word_cancels_without_ir(self):
        await enter_state(sm := build_engine(rec := Recorder()), rec, State.CONFIRMING)
        await sm.on_asr_text("不要")
        assert rec.ir == []
        assert sm.state is State.STANDBY

    async def test_unrelated_answer_restates_once_then_cancels(self):
        """Test scenario 2: 无关应答 → 复述一次 → 再次无关 → 取消，send_ir 未被调用。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CONFIRMING)

        await sm.on_asr_text("今天天气怎么样")
        assert sm.state is State.CONFIRMING
        assert rec.ir == []
        assert any(pb.CONFIRM_RESTATE == s for s in rec.spoken), "无关应答应复述提示一次"

        await sm.on_asr_text("讲个笑话")
        assert sm.state is State.STANDBY
        assert rec.ir == [], "fail-closed：语义不明一律不执行"

    async def test_timeout_10s_fail_closed(self):
        """Test scenario 2b: 10s 超时 → 不执行，回待机。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CONFIRMING)

        sm.tick(T0 + 9.9)
        assert sm.state is State.CONFIRMING, "9.9s 内不应超时"
        sm.tick(T0 + 10.1)
        assert sm.state is State.STANDBY
        assert rec.ir == []

    async def test_disabled_device_refuses_and_speaks(self):
        """AE3 given：设备被后台禁用 → 拒绝执行并语音告知。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("打开加湿器")
        assert rec.ir == []
        assert sm.state is State.STANDBY
        assert any("禁用" in s for s in rec.spoken)


# ---------- Covers AE4. READING 白名单隔离（R21/R11） ----------


class TestAe4ReadingWhitelistIsolation:
    async def test_reading_whitelist_isolation(self):
        """Test scenario 3: 白名单外文本零意图；白名单命令触发播放控制。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.READING)

        route_after_entry = rec.routes[-1]
        assert route_after_entry is AudioRoute.PLAYBACK, "进入 READING 应切白名单 KWS 路由"

        n_spoken = len(rec.spoken)
        await sm.on_asr_text("打开电视")  # 朗读内容（AE4 场景）
        await sm.on_asr_text("把空调调到26度")  # 白名单外指令
        assert rec.ir == [], "朗读内容不得进入意图通道（R11 结构性保证）"
        assert rec.playback == []
        assert sm.state is State.READING

        await sm.on_asr_text("暂停")
        assert rec.playback == ["pause"]
        await sm.on_asr_text("继续")
        assert rec.playback[-1] == "resume"
        await sm.on_asr_text("再读一遍")
        assert rec.playback[-1] == "read_again"
        await sm.on_asr_text("大声点")
        assert rec.playback[-1] == "volume_up"
        await sm.on_asr_text("小声点")
        assert rec.playback[-1] == "volume_down"

        await sm.on_asr_text("停止")
        assert sm.state is State.STANDBY
        assert rec.routes[-1] is AudioRoute.WAKE, "退出 READING 应回唤醒路由"

    async def test_reading_finished_returns_to_standby(self):
        """R22：朗读自然结束（读完）→ 回待机。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.READING)
        await sm.on_reading_finished()
        assert sm.state is State.STANDBY
        assert rec.routes[-1] is AudioRoute.WAKE


# ---------- Covers R20. 会话模型 ----------


class TestR20SessionModel:
    async def test_wake_command_done_back_to_standby_then_deaf(self):
        """Test scenario 4: 唤醒→指令→完成→回待机；完成后再说话（无唤醒词）不响应。"""
        rec = Recorder()
        sm = build_engine(rec)

        sm.tick(T0)
        await sm.on_wake()
        assert sm.state is State.LISTENING
        assert rec.spoken[-1] == pb.WAKE_ACK, "唤醒后应立即反馈（R25 音频部分）"

        await sm.on_asr_text("打开台灯")
        assert sm.state is State.STANDBY
        assert rec.ir == [("台灯", "打开")]

        spoken_after_done = len(rec.spoken)
        ir_after_done = list(rec.ir)
        await sm.on_asr_text("打开电视")  # 无唤醒词，不应响应
        assert sm.state is State.STANDBY
        assert rec.ir == ir_after_done
        assert len(rec.spoken) == spoken_after_done, "待机态 ASR 文本应被忽略（R20 无跟随窗口）"

    async def test_low_risk_command_skips_confirmation(self):
        """低危家电不需要二次确认（仅高危加热设备）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("关闭台灯")
        assert rec.ir == [("台灯", "关闭")]
        assert sm.state is State.STANDBY


# ---------- Covers R24. 识别失败引导 ----------


class TestR24RecognitionFailureGuidance:
    async def test_two_guidances_then_polite_standby(self):
        """Test scenario 5: 两次无匹配指令 → 各有一次引导话术；第 3 次礼貌回待机。"""
        rec = Recorder()
        sm = build_engine(rec)  # 无 LLM handler：全部走 UNAVAILABLE → R24 路径
        await enter_state(sm, rec, State.LISTENING)

        await sm.on_asr_text("嗯那个啥")
        assert sm.state is State.LISTENING
        guidance = [s for s in rec.spoken if "嗯那个啥" in s]
        assert len(guidance) == 1, "引导话术应说出误解内容（R24）"

        await sm.on_asr_text("呃呃呃")
        assert sm.state is State.LISTENING
        guidance = [s for s in rec.spoken if "呃呃呃" in s]
        assert len(guidance) == 1

        await sm.on_asr_text("巴拉巴拉")
        assert sm.state is State.STANDBY, "最多重试 2 次后礼貌回待机"
        assert rec.ir == []

    async def test_failure_counter_resets_per_session(self):
        rec = Recorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        await sm.on_asr_text("嗯那个啥")
        await sm.on_asr_text("呃呃呃")
        await sm.on_asr_text("巴拉巴拉")
        assert sm.state is State.STANDBY

        sm.tick(T0 + 60)
        await sm.on_wake()  # 新会话
        await sm.on_asr_text("嗯那个啥")
        assert sm.state is State.LISTENING, "新会话失败计数应清零"


# ---------- Covers R25. 聆听窗口超时 ----------


class TestR25ListenWindow:
    async def test_timeout_exits_silently_after_one_window(self):
        """唤醒后无指令，单个窗口到期即安静回待机。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)

        sm.tick(T0 + 7.9)
        assert sm.state is State.LISTENING
        sm.tick(T0 + 8.0)
        assert sm.state is State.STANDBY
        assert rec.spoken == [pb.WAKE_ACK]
        await sm.on_asr_text("帮我读下这本书")
        assert sm.state is State.STANDBY

    async def test_speech_resets_window(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        sm.tick(T0 + 5)
        await sm.on_asr_text("打开台灯")
        assert sm.state is State.STANDBY
        sm.tick(T0 + 100)  # 若窗口未清，此处会误触发超时
        assert sm.state is State.STANDBY


# ---------- Covers R31. 帮助 ----------


class TestR31Help:
    async def test_help_speaks_capability_list(self):
        """Test scenario 7: "你能做什么" → 播报能力清单文案。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")
        assert sm.state is State.FOLLOWUP
        assert any(("读" in s and "家电" in s) for s in rec.spoken), pb.HELP_TEXT


# ---------- Edge case: 任意态取消（R20） ----------


class TestCancelFromAnyState:
    @pytest.mark.parametrize("cancel_word", ["取消", "算了"])
    @pytest.mark.parametrize(
        "target",
        [State.LISTENING, State.CONFIRMING, State.CAPTURING, State.READING],
    )
    async def test_cancel_returns_to_standby(self, target: State, cancel_word: str):
        """Test scenario 8: 会话中"取消/算了"从任意态回 Standby。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, target)

        await sm.on_asr_text(cancel_word)
        assert sm.state is State.STANDBY
        if target is State.CONFIRMING:
            assert rec.ir == [], "取消后高危指令不得执行"


# ---------- 补充缺口：OCR 阅读流程态（CaptureGoal→READING）与 R26 占位 ----------


class TestCaptureFlow:
    async def test_read_intent_enters_capturing_then_reading(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("帮我读一下这个")
        assert sm.state is State.CAPTURING
        assert rec.captures == 1, "阅读意图应触发拍摄回调（U4 接入点）"

        await sm.on_capture_done(True)
        assert sm.state is State.READING

    async def test_capture_failure_guidance_max_two_then_give_up(self):
        """R26 占位：拍摄失败语音引导调整，最多 2 次后告知请家人帮忙。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("帮我读一下这个")

        await sm.on_capture_done(False)
        assert sm.state is State.CAPTURING
        assert rec.captures == 2, "失败后自动重拍一次"

        await sm.on_capture_done(False)
        assert sm.state is State.CAPTURING
        assert rec.captures == 3

        await sm.on_capture_done(False)
        assert sm.state is State.STANDBY
        assert any("家人" in s for s in rec.spoken), "仍失败应告知请家人帮忙"


# ---------- F1：取消中止在途拍摄管线 ----------


class TestCancelAbortsCapture:
    async def test_cancel_during_capture_cancels_inflight_capture_task(self):
        """F1: CAPTURING 中"取消" → 状态机回待机且取消 ensure_future 的拍摄任务。"""
        rec = Recorder()
        sm = build_engine(rec)
        holder: dict[str, asyncio.Task] = {}
        released = asyncio.Event()

        async def slow_capture() -> None:
            await released.wait()  # 模拟拍摄/OCR 在途

        def start_capture() -> object:
            task = asyncio.ensure_future(slow_capture())
            holder["task"] = task
            return task

        rec.start_capture = start_capture  # type: ignore[method-assign]
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("帮我读一下这个")
        assert sm.state is State.CAPTURING
        assert not holder["task"].done()

        await sm.on_asr_text("取消")
        assert sm.state is State.STANDBY
        released.set()  # 保险：即便取消失效也不悬挂
        for _ in range(100):
            await asyncio.sleep(0.01)
            if holder["task"].done():
                break
        assert holder["task"].cancelled(), "在途拍摄任务必须被中止（否则继续读图自行开播）"

    async def test_capture_success_clears_task_handle_before_reading_cancel(self):
        """成功进 READING 后句柄作废：READING 取消不再去 cancel 已完成拍摄任务。"""
        rec = Recorder()
        sm = build_engine(rec)
        rec.start_capture = lambda: None  # type: ignore[method-assign]
        await enter_state(sm, rec, State.READING)
        assert sm._capture_task is None
        await sm.on_asr_text("取消")
        assert sm.state is State.STANDBY  # 不因作废句柄抛异常


# ---------- 补充缺口：会话内三级路由降级 ----------


class TestLlmDegradeInSession:
    async def test_local_llm_reply_spoken_and_opens_followup(self):
        rec = Recorder()
        sm = build_engine(rec, local_llm=text_reply("这个我记下了。"))
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("提醒我明天吃药")
        assert "这个我记下了。" in rec.spoken
        assert sm.state is State.FOLLOWUP

    async def test_remote_reply_spoken(self):
        rec = Recorder()
        sm = build_engine(rec, remote_llm=text_reply("远程回答"), remote_available=lambda: True)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("复杂问题")
        assert "远程回答" in rec.spoken
        assert sm.state is State.FOLLOWUP


def text_reply(reply: str):
    async def handler(text: str) -> str | None:
        return reply

    return handler


class ControlledCompletion(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.status = "pending"

    def finish(self, status="success"):
        self.status = status
        self.set()


class ControlledRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.completions = []
        self.generation = 0
        self.stopped = []

    def speak(self, text, *, owner=None):
        self.spoken.append(text)
        done = ControlledCompletion()
        self.completions.append(done)
        return done

    def chime(self, *, owner=None):
        done = ControlledCompletion()
        done.finish()
        return done

    def input_generation(self):
        return self.generation

    def start_listening(self, *, replay=True):
        self.generation += 1
        super().start_listening()

    async def stop_audio(self, owner):
        self.stopped.append(owner)


class TestVoiceContinuity:
    async def test_continuous_noise_without_recognition_progress_recovers(self):
        rec = ControlledRecorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        sm.on_speech_started(rec.generation)
        for second in range(1, 40):
            sm.tick(T0 + second)
            sm.on_audio_progress(rec.generation)
        assert pb.INPUT_NOT_HEARD in rec.spoken
        assert sm.state is State.STANDBY

    async def test_old_capture_receipt_and_progress_do_not_affect_new_capture(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CAPTURING)
        old_owner = sm.owner
        await sm.on_wake()
        await sm.on_asr_text("帮我读下这本书")
        spoken = list(rec.spoken)
        await sm.on_capture_done(True, owner=old_owner)
        sm.speak_progress(old_owner, "旧识别完成")
        assert sm.state is State.CAPTURING
        assert rec.spoken == spoken
        await sm.on_capture_done(True, owner=sm.owner)
        assert sm.state is State.READING

    async def test_failed_stop_still_recovers_input_readiness(self):
        rec = ControlledRecorder()
        async def fail(owner):
            raise RuntimeError("driver failed")
        rec.stop_audio = fail
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        assert sm.state is State.LISTENING
        assert rec.spoken[-1] == pb.PLAYBACK_FAILED
        sm.tick(T0 + 8)
        assert sm.state is State.STANDBY

    async def test_failed_playback_recovers_without_followup(self):
        rec = ControlledRecorder()
        sm = build_engine(rec, local_llm=text_reply("回答"))
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("问题")
        rec.completions[-1].finish("failed")
        await asyncio.sleep(0)
        assert sm.state is State.LISTENING
        assert rec.spoken[-1] == pb.PLAYBACK_FAILED
        sm.tick(T0 + 8)
        assert sm.state is State.STANDBY

    async def test_missing_input_recovers_without_truncating_ongoing_speech(self):
        rec = ControlledRecorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        sm.on_speech_started(rec.generation)
        sm.tick(T0 + 29.9)
        assert sm.state is State.LISTENING
        sm.tick(T0 + 30)
        assert sm.state is State.LISTENING
        assert rec.spoken[-1] == pb.INPUT_NOT_HEARD
        sm.tick(T0 + 38)
        assert sm.state is State.STANDBY

    async def test_wake_reading_pauses_before_listening(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.READING)
        await sm.on_wake()
        assert rec.playback == ["pause"]
        assert sm.state is State.LISTENING

    async def test_cancel_pending_playback_prevents_late_followup(self):
        rec = ControlledRecorder()
        sm = build_engine(rec, local_llm=text_reply("回答"))
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("问题")
        done = rec.completions[-1]
        await sm.on_asr_text("结束聊天")
        done.finish()
        await asyncio.sleep(0)
        assert sm.state is State.STANDBY

    async def test_followup_starts_only_after_successful_playback(self):
        rec = ControlledRecorder()
        sm = build_engine(rec, local_llm=text_reply("回答"))
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("为什么天是蓝的")
        assert sm.state is State.SPEAKING
        sm.tick(T0 + 60)
        assert sm.state is State.SPEAKING
        rec.completions[-1].finish()
        await asyncio.sleep(0)
        assert sm.state is State.FOLLOWUP
        sm.tick(T0 + 74.9)
        assert sm.state is State.FOLLOWUP
        sm.tick(T0 + 75)
        assert sm.state is State.STANDBY

    async def test_valid_long_speech_is_not_cut_by_idle_window(self):
        rec = ControlledRecorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        sm.tick(T0 + 7.9)
        sm.on_speech_started(rec.generation)
        for second in range(8, 49):
            sm.tick(T0 + second)
            sm.on_audio_progress(rec.generation)
            sm.on_recognition_progress(rec.generation)
        assert sm.state is State.LISTENING
        sm.on_speech_ended(rec.generation)
        await sm.on_asr_text("打开台灯", generation=rec.generation)
        assert rec.ir == [("台灯", "打开")]

    async def test_old_route_result_after_new_wake_is_discarded(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(text):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass  # non-cancellable backend returning late
            return "迟到的旧回答"
        rec = ControlledRecorder()
        sm = build_engine(rec, local_llm=handler)
        await enter_state(sm, rec, State.LISTENING)
        old = asyncio.create_task(sm.on_asr_text("旧问题"))
        await entered.wait()
        assert sm.state is State.PROCESSING
        await sm.on_wake()
        release.set()
        await old
        assert sm.state is State.LISTENING
        assert "迟到的旧回答" not in rec.spoken

    async def test_stale_speech_and_endpoint_do_not_extend_new_session(self):
        rec = ControlledRecorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        old_generation = rec.generation
        await sm.on_wake()
        sm.on_speech_started(old_generation)
        await sm.on_asr_text("打开台灯", generation=old_generation)
        sm.tick(T0 + 8)
        assert sm.state is State.STANDBY
        assert rec.ir == []


# ---------- Covers AE1. 统一退出∪喊停（R1/R3/R5/R7；2026-10-06 计划 U3） ----------


class TestUnifiedExitStop:
    async def test_listening_exit_word_ends_session_amid_guidance(self):
        """Covers AE1. 识别失败引导循环中说「别吵我」→ 立即回待机，不再继续引导。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("今天天气怎么样")  # 无本地匹配 → R24 引导一次，仍 LISTENING
        assert sm.state is State.LISTENING
        await sm.on_asr_text("别吵我")
        assert sm.state is State.STANDBY

    async def test_speaking_exit_word_stops_playback(self):
        """播报中（SPEAKING）ASR 文本路径的退出分支完整性。"""
        rec = ControlledRecorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        await sm.on_asr_text("你能做什么")  # HELP → _speak_answer，completion 挂起 → SPEAKING
        assert sm.state is State.SPEAKING
        await sm.on_asr_text("闭嘴")
        assert sm.state is State.STANDBY

    async def test_confirming_exit_word_fails_closed_without_ir(self):
        """CONFIRMING 中说「行了」→ 与「取消」同路径：回待机、挂起高危动作不执行。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CONFIRMING)
        await sm.on_asr_text("行了")
        assert rec.ir == []
        assert sm.state is State.STANDBY

    async def test_reading_stop_word_stops_not_pauses(self):
        """Covers AE2（文本路径）。READING 中「别念了」→ playback_stop（非暂停）+ 待机。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.READING)
        await sm.on_asr_text("别念了")
        assert sm.state is State.STANDBY
        assert rec.playback[-1] == "stop"

    async def test_reading_jieshu_liaotian_keeps_pause_semantics(self):
        """「结束聊天」既有暂停语义保留（与退出词的停止语义并存）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.READING)
        await sm.on_asr_text("结束聊天")
        assert sm.state is State.STANDBY
        assert rec.playback[-1] == "pause"

    async def test_capturing_exit_word_returns_standby(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CAPTURING)
        captures_before = rec.captures
        await sm.on_asr_text("够了")
        assert sm.state is State.STANDBY
        assert rec.captures == captures_before

    async def test_followup_exit_word_ends_session(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")  # speak 返回 None → 同步完成 → FOLLOWUP
        assert sm.state is State.FOLLOWUP
        await sm.on_asr_text("够了")
        assert sm.state is State.STANDBY

    async def test_standby_ignores_exit_words(self):
        """R3：待机不监听退出说法。"""
        rec = Recorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_asr_text("退出")
        assert sm.state is State.STANDBY
        assert rec.spoken == []

    async def test_guanji_ends_session_without_guidance(self):
        """R5 会话语境承接：LISTENING 说「关机」→ 结束会话，不掉识别失败引导。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("关机")
        assert sm.state is State.STANDBY
        assert rec.spoken == [pb.WAKE_ACK]


# ---------- 真关机（R4/R5；2026-10-06 计划 U4） ----------


class TestShutdown:
    async def test_wake_prefixed_phrase_farewell_then_poweroff(self):
        """Covers AE3. 待机唤醒后说「小丽拉，立即去休息」→ 告别语 + poweroff 恰一次。"""
        rec = Recorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        await sm.on_asr_text("立即去休息", wake_prefixed=True)
        await asyncio.sleep(0)  # 让 commit 任务拿到循环槽位
        assert any("休息" in s for s in rec.spoken), "应有告别语"
        assert rec.poweroffs == 1
        assert sm.state is State.STANDBY

    async def test_poweroff_fires_after_farewell_completes(self):
        """告别语 completion 未完成时 poweroff 不触发；完成后触发（时序保证）。"""
        rec = ControlledRecorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        await sm.on_asr_text("立即去休息", wake_prefixed=True)
        assert rec.poweroffs == 0, "告别语播完前不得关机"
        rec.completions[-1].finish()
        await asyncio.sleep(0.05)
        assert rec.poweroffs == 1

    async def test_farewell_interrupted_still_commits_poweroff(self):
        """告别语被喊停截断（completion 失败/取消）→ 关机意图已提交，poweroff 照常。"""
        rec = ControlledRecorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        await sm.on_asr_text("立即去休息", wake_prefixed=True)
        rec.completions[-1].finish("failed")
        await asyncio.sleep(0.05)
        assert rec.poweroffs == 1

    async def test_bare_phrase_without_prefix_is_not_shutdown(self):
        """R4 前缀要求：会话中裸说「立即去休息」→ 普通指令处理，不关机。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("立即去休息")  # 无 wake_prefixed
        assert rec.poweroffs == 0
        assert sm.state is State.LISTENING  # 落识别失败引导

    async def test_shutdown_drops_pending_high_risk_confirm(self):
        """CONFIRMING 挂起高危确认时真关机 → 挂起丢弃、IR 不发。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CONFIRMING)
        await sm.on_asr_text("立即去休息", wake_prefixed=True)
        await asyncio.sleep(0)
        assert rec.ir == []
        assert rec.poweroffs == 1
        assert sm.state is State.STANDBY

    async def test_guanji_and_guandian_never_shutdown(self):
        """Covers AE4/R5. 「关机」（结束会话）与「关电视」（家电语境）都不关机。"""
        rec = Recorder()
        sm = build_engine(rec)
        sm.tick(T0)
        await sm.on_wake()
        await sm.on_asr_text("关机", wake_prefixed=True)  # 即便带前缀也只是结束会话
        assert rec.poweroffs == 0
        assert sm.state is State.STANDBY
        await sm.on_wake()
        await sm.on_asr_text("关电视", wake_prefixed=True)
        assert rec.poweroffs == 0


# ---------- 勿扰模式核心（R8-R14；2026-10-06 计划 U5） ----------


class CaptureTaskRecorder(Recorder):
    """start_capture 返回可取消句柄（F1 中止断言用）。"""

    def __init__(self):
        super().__init__()
        self.cancelled_tasks = []

    def start_capture(self):
        class _Task:
            def __init__(self, log):
                self._log = log

            def cancel(self):
                self._log.append("cancel")

        return _Task(self.cancelled_tasks)


async def enter_dnd(sm: DialogEngine, rec: Recorder) -> None:
    """驱动进入勿扰（会话内裸说入口），返回时处于勿扰待机。"""
    sm.tick(T0)
    await sm.on_wake()
    await sm.on_asr_text("进入勿扰模式")
    assert sm.dnd is True
    assert sm.state is State.STANDBY


class TestDndMode:
    async def test_enter_from_session_silent_with_listen_route(self):
        """Covers R8/R13. 会话内裸说进入勿扰：零提示音、待机路由切 LISTEN。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("进入勿扰模式")
        assert sm.dnd is True
        assert sm.state is State.STANDBY
        assert rec.spoken == spoken_before, "进入勿扰零提示音"
        assert rec.routes[-1] is AudioRoute.LISTEN

    async def test_enter_aborts_pending_capture(self):
        """F1 勿扰复现防线：拍摄引导中进入勿扰 → 在途拍摄中止，迟到完成不出声。"""
        rec = CaptureTaskRecorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.CAPTURING)
        await sm.on_asr_text("进入勿扰模式")
        assert rec.cancelled_tasks == ["cancel"], "在途拍摄任务必须中止"
        assert sm.dnd is True
        # 迟到的拍摄完成被态守卫丢弃，不得开播朗读
        await sm.on_capture_done(True)
        assert sm.state is State.STANDBY
        assert any("识别已完成" in s for s in rec.spoken) is False

    async def test_dnd_idle_bare_exit_phrase(self):
        """Covers R11. 勿扰中裸说退出短语 → 退勿扰 + 确认话术 + 路由回 WAKE。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_asr_text("退出勿扰模式")
        assert sm.dnd is False
        assert sm.state is State.STANDBY
        assert rec.spoken[-1] == pb.DND_EXIT_ACK
        assert rec.routes[-1] is AudioRoute.WAKE

    async def test_dnd_idle_query_answers_and_stays(self):
        """Covers R12. 勿扰中询问 → 豁免应答、仍留勿扰待机（不进 FOLLOWUP）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_asr_text("你现在是在勿扰模式吗")
        assert rec.spoken[-1] == pb.DND_STATUS_ON
        assert sm.dnd is True
        assert sm.state is State.STANDBY
        assert rec.routes[-1] is AudioRoute.LISTEN

    async def test_normal_mode_query_answers_off(self):
        """Covers R12/AE8. 正常模式询问 → 回答「不在勿扰」，走常规回答链。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你是不是在睡觉")
        assert rec.spoken[-1] == pb.DND_STATUS_OFF
        assert sm.dnd is False
        assert sm.state is State.FOLLOWUP

    async def test_dnd_idle_normal_command_ignored_silently(self):
        """Covers R9. 勿扰中普通指令：不执行、不出声。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("打开台灯")
        assert sm.state is State.STANDBY
        assert sm.dnd is True
        assert rec.spoken == spoken_before
        assert rec.ir == []

    async def test_wake_in_dnd_opens_silent_window_then_exit(self):
        """勿扰中裸唤醒词开静默聆听窗（应答门在装配层），退出短语生效。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_wake()
        assert sm.state is State.LISTENING, "唤醒后开聆听窗（提示音门在装配层）"
        await sm.on_asr_text("退出勿扰模式", wake_prefixed=True)
        assert sm.dnd is False
        assert sm.state is State.STANDBY
        assert rec.spoken[-1] == pb.DND_EXIT_ACK

    async def test_wake_in_dnd_normal_command_ignored(self):
        """勿扰聆听窗内普通指令：忽略不出声，窗口保持到超时。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_wake()
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("开电视")
        assert sm.state is State.LISTENING
        assert rec.spoken == spoken_before
        assert rec.ir == []

    async def test_shutdown_phrase_in_dnd_with_prefix_executes(self):
        """勿扰中关机短语（前缀投递）直接生效：告别语 + poweroff + 清 dnd。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_asr_text("立即去休息", wake_prefixed=True)
        await asyncio.sleep(0)
        assert rec.poweroffs == 1
        assert sm.dnd is False

    async def test_bare_shutdown_phrase_in_dnd_ignored(self):
        """勿扰中裸说「立即去休息」视同不可归一话语：不计数不触发。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("立即去休息")
        assert rec.poweroffs == 0
        assert sm.dnd is True
        assert rec.spoken == spoken_before

    async def test_rearm_after_generation_bump(self):
        """聆听再武装：dispatcher clear 推代次后，勿扰待机投递仍可达（R9 可达性）。"""
        rec = ControlledRecorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        rec.generation = 7  # 模拟隐私恢复/麦克风重连引发的 dispatcher clear
        await sm.on_asr_text("退出勿扰模式", generation=7)
        assert sm.dnd is False, "clear 后勿扰退出必须仍可达"
        assert rec.spoken[-1] == pb.DND_EXIT_ACK

    async def test_dnd_survives_privacy_toggle(self):
        """勿扰标志不受隐私开关影响（隐私门在上游切音频）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        sm.apply_privacy(True)
        assert sm.dnd is True
        sm.apply_privacy(False)
        assert sm.dnd is True

    async def test_enter_phrase_in_dnd_idempotent(self):
        """勿扰中重复进入短语 = 幂等 no-op（忽略不出声）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("进入勿扰模式")
        assert sm.dnd is True
        assert sm.state is State.STANDBY
        assert rec.spoken == spoken_before


# ---------- 勿扰三次逃生舱（R10；2026-10-06 计划 U6） ----------


class TestDndEscapeHatch:
    async def _drive_to_prompt(self, sm: DialogEngine, rec: Recorder, word="台灯", action="打开") -> None:
        """勿扰中同义指令×3（换说法）→ 主动开口询问（Covers AE6 触发侧）。"""
        await enter_dnd(sm, rec)
        for text in (f"{action}{word}", f"把{word}{action}", f"帮我{action}{word}"):
            await sm.on_asr_text(text)
        assert sm._dnd_escape is not None
        assert sm._dnd_escape.stage == "exit"
        assert rec.spoken[-1] == pb.dnd_escape_prompt_text(f"{action}{word}")

    async def test_three_same_intent_triggers_prompt(self):
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        assert sm.dnd is True and sm.state is State.STANDBY

    async def test_different_command_resets_counter(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_asr_text("打开台灯")
        await sm.on_asr_text("把台灯打开")
        await sm.on_asr_text("关闭台灯")  # 不同意图 → 清零重计
        assert sm._dnd_escape is None
        await sm.on_asr_text("关闭台灯")
        assert sm._dnd_escape is None, "仅 2 次，不得触发"
        await sm.on_asr_text("把台灯关闭")
        assert sm._dnd_escape is not None, "关闭台灯第 3 次触发"

    async def test_unnormalizable_neither_counts_nor_resets(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_asr_text("打开台灯")
        await sm.on_asr_text("今天天气怎么样")  # LLM 层：不累加不清零（裁定 12）
        await sm.on_asr_text("把台灯打开")
        assert sm._dnd_escape is None, "仅 2 次"
        await sm.on_asr_text("帮我打开台灯")
        assert sm._dnd_escape is not None, "闲聊不打断计数，第 3 次触发"

    async def test_exit_agree_low_risk_then_executes(self):
        """Covers AE6. 询问→确认→执行问句→开吧→执行 IR。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        await sm.on_asr_text("确认")
        assert sm.dnd is False
        assert sm.state is State.LISTENING
        assert rec.spoken[-1] == pb.dnd_exec_prompt_text("打开台灯")
        await sm.on_asr_text("开吧")
        assert rec.ir == [("台灯", "打开")]
        assert any("已发出" in s for s in rec.spoken)
        assert sm.state is State.STANDBY

    async def test_exit_agree_high_risk_folds_into_named_confirm(self):
        """Covers R21. 高危折叠：执行问句直接用具名高危确认话术，确认后走同一闸。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        for text in ("打开取暖器", "把取暖器打开", "帮我打开取暖器"):
            await sm.on_asr_text(text)
        assert rec.spoken[-1] == pb.dnd_escape_prompt_text("打开取暖器")
        await sm.on_asr_text("确认")
        assert sm.dnd is False
        assert rec.spoken[-1] == pb.confirm_prompt_text("打开取暖器"), "高危折叠为具名确认"
        await sm.on_asr_text("确认")
        assert rec.ir == [("取暖器", "打开")]
        assert sm.state is State.STANDBY

    async def test_exit_refuse_stays_dnd_and_resets_counter(self):
        """Covers AE7. 答「不用」→ 静默回勿扰、计数清零（重攒三次才再问）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        spoken_after_prompt = list(rec.spoken)
        await sm.on_asr_text("不用")
        assert sm.dnd is True
        assert sm._dnd_escape is None
        assert rec.spoken == spoken_after_prompt, "拒绝后静默回勿扰"
        await sm.on_asr_text("打开台灯")
        assert sm._dnd_escape is None, "计数已清零，1 次不得触发"

    async def test_exit_timeout_keeps_counter_reasks_immediately(self):
        """裁定 6：超时回勿扰、计数保持——下次同义指令立即重问。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        sm.tick(T0 + 10.1)
        assert sm._dnd_escape is None
        assert sm.dnd is True
        await sm.on_asr_text("打开台灯")
        assert sm._dnd_escape is not None, "计数保持，单次同义立即重问"
        assert rec.spoken[-1] == pb.dnd_escape_prompt_text("打开台灯")

    async def test_answer_exit_phrase_skips_exec_question(self):
        """裁定 6：逃生询问中答「退出勿扰模式」= 同意退出且跳过执行问句。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        await sm.on_asr_text("退出勿扰模式")
        assert sm.dnd is False
        assert sm.state is State.STANDBY
        assert rec.spoken[-1] == pb.DND_EXIT_ACK
        assert rec.ir == []

    async def test_exec_refuse_normal_standby_no_ir(self):
        """裁定 8：执行问句被拒 → 正常待机不执行（已同意退出，勿扰不回）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        await sm.on_asr_text("确认")
        await sm.on_asr_text("不要")
        assert sm.dnd is False
        assert sm.state is State.STANDBY
        assert rec.ir == []

    async def test_exec_timeout_normal_standby_no_ir(self):
        """执行问句超时 → 正常待机不执行。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        await sm.on_asr_text("确认")
        sm.tick(T0 + 10.1)
        assert sm._dnd_escape is None
        assert sm.state is State.STANDBY
        assert rec.ir == []

    async def test_unrelated_answer_treated_as_refusal(self):
        """裁定 6：无关应答按拒绝处理（fail-closed 静默回勿扰）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        spoken_after_prompt = list(rec.spoken)
        await sm.on_asr_text("现在几点")
        assert sm.dnd is True
        assert sm._dnd_escape is None
        assert rec.spoken == spoken_after_prompt


# ---------- 追问窗「再说一遍」（R15；2026-10-06 计划 U7） ----------


class TestRepeatLastAnswer:
    async def test_repeat_replays_last_answer_verbatim(self):
        """Covers AE10. 回答播完追问窗内「再说一遍」→ 原文重报、不重新生成。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")  # HELP 回答 → 同步完成 → FOLLOWUP
        assert sm.state is State.FOLLOWUP
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("再说一遍")
        assert rec.spoken[-1] == pb.HELP_TEXT, "原文重报"
        assert sm.state is State.FOLLOWUP, "重报后重开追问窗"
        assert rec.spoken[len(spoken_before):].count(pb.HELP_TEXT) == 1, "恰重报一次"

    async def test_repeat_twice_same_text_history_not_duplicated(self):
        """窗内连续重报同文；重报不写入历史（上下文不自污染）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")
        pairs_after_answer = len(sm._history.snapshot().pairs)
        await sm.on_asr_text("再说一遍")
        await sm.on_asr_text("再说一遍")
        assert rec.spoken[-1] == pb.HELP_TEXT
        assert len(sm._history.snapshot().pairs) == pairs_after_answer, "重报不入史"

    async def test_repeat_in_listening_falls_through(self):
        """非追问窗（LISTENING）说「再说一遍」→ 按普通指令路由（识别失败引导）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("再说一遍")
        assert sm.state is State.LISTENING
        assert any("没听清" in s or "试试" in s for s in rec.spoken), "走 R24 引导"

    async def test_repeat_with_empty_history_falls_through(self):
        """FOLLOWUP 但历史无成功回答 → 按普通指令路由。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")
        assert sm.state is State.FOLLOWUP
        sm._history.clear()
        await sm.on_asr_text("再说一遍")
        assert any("没听清" in s or "试试" in s for s in rec.spoken), "无历史走引导"


# ---------- 全局音量语音控制（R16；2026-10-06 计划 U8） ----------


class TestVoiceVolume:
    async def test_volume_up_in_listening_adjusts_and_acks(self):
        """Covers AE11（非 SPEAKING 态）。「声音大点」→ +0.2 调整 + 确认话术。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("声音大点")
        assert rec.volume_deltas == [0.2]
        assert rec.spoken[-1] == pb.VOLUME_ACK
        assert sm.state is State.FOLLOWUP, "确认走回答链，重开追问窗"

    async def test_volume_down_and_clamp_is_assembly_concern(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("声音小一点")
        assert rec.volume_deltas == [-0.2]

    async def test_volume_word_in_dnd_ignored(self):
        """勿扰中音量词=不可归一话语：不出声不调整。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("声音大点")
        assert rec.volume_deltas == []
        assert rec.spoken == spoken_before


# ---------- 语音状态自查（R17；2026-10-06 计划 U9） ----------


class StatusRecorder(Recorder):
    def __init__(self, facts=None):
        super().__init__()
        self._facts = facts or {}

    def status_facts(self):
        return self._facts


class TestStatusQuery:
    async def test_query_reports_network_ok(self):
        """「你连上网了吗」→ 播报网络状态（facts mock 断言映射）。"""
        rec = StatusRecorder({"network": True, "cloud": True, "mic_on": True})
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你连上网了吗")
        assert "网络正常" in rec.spoken[-1]
        assert "云端服务也连着" in rec.spoken[-1]
        assert sm.state is State.FOLLOWUP

    async def test_query_reports_mic_off(self):
        """facts 注入麦克风关闭 → 话术含对应表述（facts 映射单测——隐私开启时
        语音路径按设计不可达：隐私门上游丢弃输入，不写语音可达断言）。"""
        rec = StatusRecorder({"network": False, "cloud": None, "mic_on": False})
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("网络怎么样")
        assert "连不上网" in rec.spoken[-1]
        assert "麦克风已经关闭" in rec.spoken[-1]
        assert "云端" not in rec.spoken[-1], "未知事实降级省略，不误报"

    async def test_query_in_dnd_exempt_and_stays(self):
        """裁定 9：勿扰中询问 → 豁免应答、仍留勿扰待机。"""
        rec = StatusRecorder({"network": True, "cloud": None, "mic_on": True})
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        await sm.on_asr_text("你连上网了吗")
        assert "网络正常" in rec.spoken[-1]
        assert sm.dnd is True
        assert sm.state is State.STANDBY

    async def test_facts_getter_missing_degrades(self):
        """可选回调缺失（旧 Recorder）→ 空 facts 降级表述，播报不阻塞。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你连上网了吗")
        assert "说不准" in rec.spoken[-1]
        assert sm.state is State.FOLLOWUP


# ---------- Tier-2 评审修复回归（ADV-01/02/04、TST-001） ----------


class TestDndEscapeHighRiskLegs(TestDndEscapeHatch):
    """TST-001：高危折叠的拒绝/超时腿用高危设备钉死（低危版本见父类）。"""

    async def test_exec_refuse_high_risk_no_ir(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        for text in ("打开取暖器", "把取暖器打开", "帮我打开取暖器"):
            await sm.on_asr_text(text)
        await sm.on_asr_text("确认")
        await sm.on_asr_text("不要")
        assert rec.ir == [], "高危折叠拒绝腿 fail-closed"
        assert sm.state is State.STANDBY
        assert sm.dnd is False

    async def test_exec_timeout_high_risk_no_ir(self):
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        for text in ("打开取暖器", "把取暖器打开", "帮我打开取暖器"):
            await sm.on_asr_text(text)
        await sm.on_asr_text("确认")
        sm.tick(T0 + 10.1)
        assert rec.ir == [], "高危折叠超时腿 fail-closed"
        assert sm.state is State.STANDBY


class TestReviewFixes(TestDndEscapeHatch):
    async def test_wake_during_exit_prompt_cancels_escape(self):
        """ADV-01A. 唤醒即接管：逃生对话作废（聆听窗正常武装可超时，不再永久开放）；
        勿扰标志保持——静默聆听窗内指令仍按勿扰规则计数忽略（U5 规格）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        await sm.on_wake()
        assert sm._dnd_escape is None
        assert sm.state is State.LISTENING
        assert sm.dnd is True
        # 窗内指令仍按勿扰规则处理（不劫持），退出短语正常生效
        await sm.on_asr_text("开吧")
        assert rec.ir == []
        await sm.on_asr_text("退出勿扰模式")
        assert sm.dnd is False
        assert sm.state is State.STANDBY

    async def test_wake_during_exec_stage_abandons_command(self):
        """ADV-01B. exec 阶段唤醒 → 放弃执行；他人口语「开吧」不发射 IR。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        await sm.on_asr_text("确认")
        assert sm._dnd_escape is not None and sm._dnd_escape.stage == "exec"
        await sm.on_wake()
        assert sm._dnd_escape is None
        await sm.on_asr_text("开吧")
        assert rec.ir == [], "已放弃的逃生舱指令不得在新会话执行"

    async def test_wake_prefixed_shutdown_not_hijacked_by_escape(self):
        """ADV-01C. exec 阶段唤醒后「小丽拉立即去休息」→ 正常关机而非被逃生舱吞掉。"""
        rec = Recorder()
        sm = build_engine(rec)
        await self._drive_to_prompt(sm, rec)
        await sm.on_asr_text("确认")
        await sm.on_wake()
        await sm.on_asr_text("立即去休息", wake_prefixed=True)
        await asyncio.sleep(0)
        assert rec.poweroffs == 1
        assert any("休息" in s for s in rec.spoken)

    async def test_status_query_requires_question_marker(self):
        """ADV-02. 话题词无疑问标记不劫持（指令照常路由，状态播报不抢答）。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("帮我上网查一下天气")
        assert sm.state is State.LISTENING
        assert not any("网络" in s for s in rec.spoken)
        from lira.dialog.intents import is_status_query
        assert not is_status_query("信号不好")
        assert not is_status_query("帮我上网查一下")

    async def test_dnd_ambient_topic_word_stays_silent(self):
        """ADV-02. 勿扰中环境语音含话题词但无疑问标记 → 静默忽略不计数。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_dnd(sm, rec)
        spoken_before = list(rec.spoken)
        await sm.on_asr_text("这个信号不好")
        assert rec.spoken == spoken_before
        assert sm.dnd is True

    async def test_volume_ack_not_recorded_into_history(self):
        """ADV-04. 音量罐头确认不入史：「再说一遍」重报最近真实回答。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")
        await sm.on_asr_text("声音大点")
        await sm.on_asr_text("再说一遍")
        assert rec.spoken[-1] == pb.HELP_TEXT, "重报真实回答而非「好的」"


# ---------- U3/KTD3：utterance→turn→operation 同轮观测 ----------


def _tel_events(tel, kind=None):
    items = tel.events_since(0)[0]
    return [e for e in items if kind is None or e.kind == kind]


class TestTurnTelemetry:
    """遥测旁路事件：轮次分配、实际路由、播报结束与静默忽略边界。

    断言走 DevTelemetry 公开 add_event/events_since 面（KTD3：事件带
    turn_id/utterance_id/operation_owner 关联字段，U5 前端按它渲染）。
    """

    async def _listening_engine(self, router_kwargs=None):
        from lira.dev_telemetry import DevTelemetry
        rec = Recorder()
        sm = build_engine(rec, **(router_kwargs or {}))
        tel = DevTelemetry()
        sm.telemetry = tel
        await enter_state(sm, rec, State.LISTENING)
        base = len(_tel_events(tel, "turn"))  # 唤醒本身不产生轮次
        assert base == 0
        return sm, rec, tel

    async def test_local_appliance_has_turn_route_and_no_model_events(self):
        """AE3：本地家电指令——turn+route(LOCAL_RULE) 事件齐全，远程 handler
        全程未被触碰（无模型等待/回复事件）。"""
        sm, rec, tel = await self._listening_engine(
            {"remote_llm": self._recording_remote()[0], "remote_available": lambda: True})
        await sm.on_asr_text("打开台灯", utterance_id=7)
        assert rec.ir == [("台灯", "打开")]
        kinds = [e.kind for e in _tel_events(tel)]
        assert kinds == ["turn", "route"]
        turn = _tel_events(tel, "turn")[0]
        assert turn.text == "打开台灯"
        assert turn.turn_id == 1 and turn.utterance_id == 7
        assert turn.operation_owner == "1.1"  # wake 会话 owner（epoch.operation）
        route = _tel_events(tel, "route")[0]
        assert route.text == "local_rule" and route.turn_id == turn.turn_id

    @staticmethod
    def _recording_remote():
        calls = []

        async def remote(text, **kwargs):
            calls.append(text)
            return "模型回复"

        return remote, calls

    async def test_remote_reply_route_event_and_owner_progression(self):
        """普通模型路径：route 事件记 remote_llm；turn 关联接受时的 owner。"""
        remote, calls = self._recording_remote()
        sm, rec, tel = await self._listening_engine(
            {"remote_llm": remote, "remote_available": lambda: True})
        await sm.on_asr_text("复杂问题")
        assert calls == ["复杂问题"]
        assert _tel_events(tel, "route")[0].text == "remote_llm"
        assert [e.text for e in _tel_events(tel, "turn")] == ["复杂问题"]

    async def test_standby_and_reading_whitelist_miss_emit_no_turn(self):
        """静默忽略不分配轮次：STANDBY 未唤醒文本、READING 白名单外文本
        均无 turn 事件；白名单命中才分配。"""
        from lira.dev_telemetry import DevTelemetry
        rec = Recorder()
        sm = build_engine(rec)
        tel = DevTelemetry()
        sm.telemetry = tel
        await sm.on_asr_text("打开电视")  # STANDBY：未唤醒，忽略
        assert _tel_events(tel, "turn") == []
        await enter_state(sm, rec, State.READING)
        baseline = _tel_events(tel, "turn")  # 进入 READING 的设置语句已计一轮
        assert [e.text for e in baseline] == ["帮我读一下这个"]
        await sm.on_asr_text("把空调调到26度")  # 白名单外：朗读内容不进意图通道
        assert _tel_events(tel, "turn") == baseline  # 未新增轮次
        await sm.on_asr_text("暂停")  # 白名单命中：接受并分配轮次
        assert [e.text for e in _tel_events(tel, "turn")] == [
            "帮我读一下这个", "暂停"]
        assert len(_tel_events(tel, "route")) == 1  # 播放控制不重新路由

    async def test_cancel_word_is_accepted_turn_without_route(self):
        from lira.dev_telemetry import DevTelemetry
        rec = Recorder()
        sm = build_engine(rec)
        tel = DevTelemetry()
        sm.telemetry = tel
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("取消")
        assert sm.state is State.STANDBY
        assert [e.kind for e in _tel_events(tel)] == ["turn"]

    async def test_speak_done_binds_turn_and_owner(self):
        rec = Recorder()
        sm = build_engine(rec)
        from lira.dev_telemetry import DevTelemetry
        tel = DevTelemetry()
        sm.telemetry = tel
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")  # HELP → _speak_answer → 同步完成
        turn = _tel_events(tel, "turn")[0]
        done = _tel_events(tel, "speak_done")[0]
        assert done.turn_id == turn.turn_id
        assert done.utterance_id == turn.utterance_id
        assert done.operation_owner is not None
        assert done.text  # 回答预览非空

    async def test_playback_failure_emits_speak_interrupt(self):
        from types import SimpleNamespace
        from lira.dev_telemetry import DevTelemetry

        class FailingSpeak(Recorder):
            def speak(self, text):
                self.spoken.append(text)
                return SimpleNamespace(status="failed", is_set=lambda: True)

        rec = FailingSpeak()
        sm = build_engine(rec)
        tel = DevTelemetry()
        sm.telemetry = tel
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么")
        turn = _tel_events(tel, "turn")[0]
        interrupt = _tel_events(tel, "speak_interrupt")[0]
        assert interrupt.turn_id == turn.turn_id  # 归因到本轮，不漂移

    async def test_identity_intercept_correlates_utterance_without_turn(self):
        """身份拦截路径（main.handle_voice_input）：不分配 turn，播报结束事件
        只按 utterance/owner 追溯——且不误挂上一语句的轮次。"""
        rec = Recorder()
        sm = build_engine(rec)
        from lira.dev_telemetry import DevTelemetry
        tel = DevTelemetry()
        sm.telemetry = tel
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("你能做什么", utterance_id=3)  # 正常轮
        turn1 = _tel_events(tel, "turn")[0].turn_id
        # 下一语句被身份拦截消费（同构 main.py 调用序）
        sm.note_utterance(9)
        sm._speak_answer("好，以前的聊天记忆已经清掉了。", owner=sm.owner,
                         user_text="忘记我的记忆", record=False)
        done = _tel_events(tel, "speak_done")[-1]
        assert done.utterance_id == 9
        assert done.turn_id is None  # 不挂到上一轮（turn1）
        assert _tel_events(tel, "turn")[0].turn_id == turn1  # 未新增轮次

    async def test_old_owner_speak_done_never_pollutes_next_turn(self):
        """连续两轮：旧 owner 的播报在新会话接管后完成，不落结束事件、
        不污染第二轮（第二轮 speak_done 只归因自己的 turn）。"""
        from types import SimpleNamespace
        from lira.dev_telemetry import DevTelemetry

        class SlowSpeak(Recorder):
            def __init__(self):
                super().__init__()
                self.gate = asyncio.Event()

            def speak(self, text):
                self.spoken.append(text)
                self.gate = asyncio.Event()
                return SimpleNamespace(status="success", is_set=self.gate.is_set,
                                       wait=self.gate.wait)

        rec = SlowSpeak()
        sm = build_engine(rec)
        tel = DevTelemetry()
        sm.telemetry = tel
        sm.tick(T0)
        await sm.on_wake()
        await sm.on_asr_text("你能做什么")  # 第一轮：播报挂起（completion 未定）
        assert sm.state is State.SPEAKING
        assert _tel_events(tel, "speak_done") == []
        turn1 = _tel_events(tel, "turn")[0].turn_id
        await sm.on_wake()  # 新会话接管：旧 owner 失效，完成等待被取消
        rec.gate.set()  # 旧播报此刻才完成
        await asyncio.sleep(0.01)
        assert _tel_events(tel, "speak_done") == []  # 旧 owner 不落结束事件
        await sm.on_asr_text("你能做什么")  # 第二轮
        turn2 = _tel_events(tel, "turn")[-1].turn_id
        assert turn2 != turn1
        rec.gate.set()  # 第二轮播报完成
        await asyncio.sleep(0.01)
        dones = _tel_events(tel, "speak_done")
        assert [e.turn_id for e in dones] == [turn2]
        assert [e.turn_id for e in _tel_events(tel, "turn")] == [turn1, turn2]


async def test_wait_feedback_once_and_never_after_answer_chunk():
    async def remote(text, *, on_wait_feedback=None, on_reply_chunk=None, **kwargs):
        on_wait_feedback("嗯。")
        on_wait_feedback("重复提示")
        on_reply_chunk("答案。")
        on_wait_feedback("迟到提示")
        return "答案。"
    rec = Recorder()
    sm = build_engine(rec, remote_llm=remote, remote_available=lambda: True)
    await enter_state(sm, rec, State.LISTENING)
    await sm.on_asr_text("复杂问题")
    assert rec.spoken.count("嗯。") == 1
    assert "重复提示" not in rec.spoken
    assert "迟到提示" not in rec.spoken


async def test_wait_feedback_after_owner_invalidation_is_silent():
    async def remote(text, *, on_wait_feedback=None, **kwargs):
        sm.apply_privacy(True)
        on_wait_feedback("迟到提示")
        return "答案。"
    rec = Recorder()
    sm = build_engine(rec, remote_llm=remote, remote_available=lambda: True)
    await enter_state(sm, rec, State.LISTENING)
    await sm.on_asr_text("复杂问题")
    assert "迟到提示" not in rec.spoken
    assert "答案。" not in rec.spoken
