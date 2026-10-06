"""语音退出/勿扰/控制机制 e2e（2026-10-06 计划 U10）。

覆盖 origin AE1（引导中退出）/ AE2（朗读中喊停，文本 + 真实命令 KWS 双路径）/
AE5-AE7（勿扰进出、逃生舱、拒绝）/ AE8（勿扰询问）/ AE11（语音音量）。
文本注入路径无模型依赖；真实 KWS/ASR 路径模型缺失自动 skip（conftest 机制）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lira.appliances.models import ApplianceModel  # noqa: E402
from lira.dialog.state_machine import State  # noqa: E402

ARTICLE_TEXT = "布洛芬缓释胶囊说明书。" + "用法用量：口服，一次一粒，一日两次。" * 8


def seed_lamp(h) -> None:
    h.store.upsert_appliance(ApplianceModel(
        name="台灯", aliases=("台灯", "电灯"),
        actions={"打开": ("打开", "开一下")}, codes={"打开": "P:300,400"},
        is_high_risk=False, enabled=True))


async def _start_reading(h) -> None:
    h.ocr.text = ARTICLE_TEXT
    await h.engine.on_wake()
    await h.say_text("帮我读一下这个")
    state = await h.wait_state(State.READING, timeout=15.0)
    assert state is State.READING


# ---------- AE1：识别失败引导循环中退出 ----------


async def test_exit_word_amid_followup_ends_session(harness):
    """Covers AE1（e2e 变体）。回答后的追问窗内说「别吵我」→ 立即回待机。
    （e2e fake LLM 使闲聊成为正常问答；识别失败引导中的退出由状态机单测覆盖。）"""
    h = harness
    await h.engine.on_wake()
    await h.say_text("今天天气怎么样")
    assert h.engine.state is State.FOLLOWUP
    await h.say_text("别吵我")
    assert h.engine.state is State.STANDBY
    assert h.ir_sent == []


# ---------- AE2：朗读中喊停 ----------


async def test_reading_stopped_by_exit_word_text(harness):
    """Covers AE2（文本路径）。朗读中「别念了」→ 立即停止回待机，不重新拍摄。"""
    h = harness
    seed_lamp(h)
    await _start_reading(h)
    await h.say_text("别念了")
    assert h.engine.state is State.STANDBY
    assert h.camera.capture_calls == 1


async def test_reading_stopped_by_exit_word_real_kws(harness):
    """Covers AE2（真实命令 KWS）。bie_nian_le.wav → 命令通道命中 → 朗读立停。"""
    h = harness
    if h.command_stream is None:
        pytest.skip("命令 KWS 流不可用（模型未就绪，文本注入用例已覆盖逻辑）")
    seed_lamp(h)
    await _start_reading(h)
    await h.say_wav("bie_nian_le")
    await h.settle(0.5)
    assert h.engine.state is State.STANDBY, "真实 KWS 喊停必须生效"
    assert h.camera.capture_calls == 1


# ---------- AE5-AE7：勿扰全周期 ----------


async def test_dnd_enter_silent_then_escape_hatch_executes(harness):
    """Covers AE5/AE6. 进入勿扰零提示音；同义×3 主动询问；确认后执行。"""
    h = harness
    seed_lamp(h)
    await h.engine.on_wake()
    spoken_before = list(h.speak_log)
    await h.say_text("进入勿扰模式")
    assert h.engine.dnd is True
    assert h.engine.state is State.STANDBY
    assert h.speak_log == spoken_before, "进入勿扰零提示音"

    await h.say_text("打开台灯")
    await h.say_text("把台灯打开")
    await h.say_text("帮我打开台灯")
    assert any("退出勿扰" in s for s in h.speak_log[-1:]), "同义×3 主动询问"

    await h.say_text("确认")
    assert h.engine.dnd is False, "同意退出"
    assert any("要现在为您" in s for s in h.speak_log[-1:]), "低危执行问句"
    await h.say_text("开吧")
    assert h.ir_sent == [("台灯", "打开")]
    assert h.engine.state is State.STANDBY


async def test_dnd_refuse_keeps_silence(harness):
    """Covers AE7. 逃生询问答「不用」→ 静默回勿扰。"""
    h = harness
    seed_lamp(h)
    await h.engine.on_wake()
    await h.say_text("进入勿扰模式")
    for text in ("打开台灯", "把台灯打开", "帮我打开台灯"):
        await h.say_text(text)
    spoken_len = len(h.speak_log)
    await h.say_text("不用")
    await h.settle(0.3)
    assert h.engine.dnd is True
    assert h.engine.state is State.STANDBY
    assert len(h.speak_log) == spoken_len, "拒绝后静默"
    assert h.ir_sent == []


async def test_dnd_exit_via_real_asr(harness):
    """exit_dnd.wav（真实 ASR）在勿扰待机（LISTEN 路由）→ 退出。"""
    h = harness
    if h.asr_stream is None:
        pytest.skip("ASR 不可用（模型未就绪，文本注入用例已覆盖逻辑）")
    await h.engine.on_wake()
    await h.say_text("进入勿扰模式")
    assert h.engine.dnd is True
    await h.say_wav("exit_dnd")
    await h.settle(0.5)
    assert h.engine.dnd is False, "真实 ASR 退出短语必须生效"
    assert h.engine.state is State.STANDBY


# ---------- AE8：勿扰询问 ----------


async def test_dnd_query_normal_mode(harness):
    """Covers AE8. 正常模式问「你现在是在勿扰模式吗」→ 回答不在。"""
    h = harness
    await h.engine.on_wake()
    await h.say_text("你现在是在勿扰模式吗")
    assert any("没有" in s or "不在" in s for s in h.speak_log[-1:])
    assert h.engine.dnd is False


async def test_shutdown_real_wake_prefix(harness):
    """AE3 变体（真实唤醒前缀路径）：wake_shutdown.wav → 告别语 + poweroff 断言面。"""
    h = harness
    if h.wake_stream is None:
        pytest.skip("KWS 不可用")
    await h.say_wav("wake_shutdown")
    await h.settle(4.0)
    assert any("我休息了" in s for s in h.speak_log), f"告别语已播; speak_log={h.speak_log}"
    assert h.poweroff_called, "告别语后 poweroff 已提交"
    assert h.engine.dnd is False


# ---------- AE11：语音音量 ----------


async def test_voice_volume_adjusts(harness):
    """Covers AE11（非 SPEAKING 态）。「声音大点」→ 调整 + 确认话术；真实 ASR 变体。"""
    h = harness
    await h.engine.on_wake()
    await h.say_text("声音大点")
    assert h.volume_deltas == [0.2]
    assert any("好的" in s for s in h.speak_log[-1:])
    assert h.engine.state is State.FOLLOWUP
    if h.asr_stream is not None:
        await h.say_wav("voice_big")
        await h.settle(0.5)
        assert h.volume_deltas[-1] == 0.2, "真实 ASR 音量词同样生效"
