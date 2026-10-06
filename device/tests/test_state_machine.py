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
