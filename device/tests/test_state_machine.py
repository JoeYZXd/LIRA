"""U3 对话状态机测试（迁移表，计划 Test scenarios 8 条 + 补充缺口）。

覆盖：AE3 高危确认 happy/error/超时（fail-closed）、AE4 READING 白名单隔离、
R20 会话模型（一次唤醒=一条指令）、R24 识别失败引导（≤2 次后礼貌回待机）、
R25 聆听窗口（8s 超时复述一次）、R31 帮助、任意态"取消/算了"、
CaptureGoal→READING 拍摄流程占位（R22/R26 接口）、会话内 LLM 降级。

纯逻辑状态机：无 I/O，动作经 Recorder 回调注入；时钟经 tick(now) 注入（虚拟时钟）。
"""

from __future__ import annotations

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
    async def test_timeout_reminds_once_then_standby(self):
        """Test scenario 6: 8s 无语音 → 复述提示一次（重开窗口）→ 再超时 → 待机。"""
        rec = Recorder()
        sm = build_engine(rec)
        await enter_state(sm, rec, State.LISTENING)

        sm.tick(T0 + 8.1)
        assert sm.state is State.LISTENING, "第一次超时应复述提示并继续聆听"
        assert any(pb.LISTEN_REMIND == s for s in rec.spoken)

        sm.tick(T0 + 16.2)
        assert sm.state is State.STANDBY, "第二次超时回待机"
        reminds = [s for s in rec.spoken if s == pb.LISTEN_REMIND]
        assert len(reminds) == 1, "复述提示只出现一次"

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
        assert sm.state is State.STANDBY
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


# ---------- 补充缺口：会话内三级路由降级 ----------


class TestLlmDegradeInSession:
    async def test_local_llm_reply_spoken_and_session_ends(self):
        rec = Recorder()
        sm = build_engine(rec, local_llm=text_reply("这个我记下了。"))
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("提醒我明天吃药")
        assert "这个我记下了。" in rec.spoken
        assert sm.state is State.STANDBY

    async def test_remote_reply_spoken(self):
        rec = Recorder()
        sm = build_engine(rec, remote_llm=text_reply("远程回答"), remote_available=lambda: True)
        await enter_state(sm, rec, State.LISTENING)
        await sm.on_asr_text("复杂问题")
        assert "远程回答" in rec.spoken
        assert sm.state is State.STANDBY


def text_reply(reply: str):
    async def handler(text: str) -> str | None:
        return reply

    return handler
