"""AE3：高危家电（取暖器）的红外安全链（R6/R23/R7/R25）。

- 二次确认：说"确认"才发 IR；"不要"/模糊应答/10s 超时一律不执行（fail-closed）。
- 后台禁用（enabled=False 同步落库后）：拒绝执行并播报"已被家人禁用"。
- 低危家电（台灯）无确认直发（对照）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys_path = Path(__file__).resolve().parent
if str(sys_path) not in sys.path:
    sys.path.insert(0, str(sys_path))

from lira.appliances.models import ApplianceModel  # noqa: E402
from lira.dialog import phrasebook as pb  # noqa: E402
from lira.dialog.state_machine import State  # noqa: E402

CONFIRM_WINDOW = 10.0  # R25 确认窗口（状态机常量）


def seed_appliances(h, *, heater_enabled: bool = True) -> None:
    h.store.upsert_appliance(ApplianceModel(
        name="取暖器", aliases=("取暖器", "电暖器"),
        actions={"power": ("打开",)}, codes={"power": "P:100,200"},
        is_high_risk=True, enabled=heater_enabled))
    h.store.upsert_appliance(ApplianceModel(
        name="台灯", aliases=("台灯",),
        actions={"power": ("打开",)}, codes={"power": "P:300,400"},
        is_high_risk=False, enabled=True))


async def _wake_and_command(h, text: str = "打开取暖器"):
    await h.engine.on_wake()
    await h.say_text(text)


# ---------- 确认放行 ----------


async def test_high_risk_requires_confirm_then_sends_ir(harness):
    h = harness
    seed_appliances(h)
    await _wake_and_command(h)
    assert h.engine.state is State.CONFIRMING, "高危必须进入确认态"
    assert "确认" in h.speak_log[-1], "应播报二次确认引导"
    assert h.ir_sent == [], "确认前不得发 IR"
    await h.say_text("确认")
    await h.settle(0.3)
    assert h.engine.state is State.STANDBY
    assert ("取暖器", "power") in h.ir_sent
    assert "P:100,200" in h.ir.sent_codes, "第二道闸后应发出真实码值"


async def test_low_risk_sends_ir_without_confirm(harness):
    h = harness
    seed_appliances(h)
    await h.engine.on_wake()
    await h.say_text("打开台灯")
    await h.settle(0.3)
    assert h.engine.state is State.STANDBY
    assert ("台灯", "power") in h.ir_sent


# ---------- 确认拒绝路径（fail-closed） ----------


async def test_reject_word_cancels_without_ir(harness):
    h = harness
    seed_appliances(h)
    await _wake_and_command(h)
    assert h.engine.state is State.CONFIRMING
    await h.say_text("不要")
    await h.settle(0.2)
    assert h.engine.state is State.STANDBY
    assert h.ir_sent == []


async def test_vague_answer_fails_closed(harness):
    h = harness
    seed_appliances(h)
    await _wake_and_command(h)
    assert h.engine.state is State.CONFIRMING
    # 无关应答（R23）：第一次复述确认提示（仍不发 IR），第二次取消（fail-closed）
    await h.say_text("今天天气怎么样")
    await h.settle(0.2)
    assert h.engine.state is State.CONFIRMING, "首次模糊应答复述提示"
    assert h.ir_sent == []
    assert any("确认" in t for t in h.speak_log[1:]), "复述提示应包含确认引导"
    await h.say_text("随便")  # 再次不明 → 取消
    await h.settle(0.2)
    assert h.engine.state is State.STANDBY
    assert h.ir_sent == []


async def test_confirm_timeout_fails_closed(harness):
    h = harness
    seed_appliances(h)
    await _wake_and_command(h)
    assert h.engine.state is State.CONFIRMING
    h.advance(CONFIRM_WINDOW + 0.5)  # 虚拟时钟快进（静音填充）
    await h.settle(0.3)
    assert h.engine.state is State.STANDBY, "10s 超时必须回待机"
    assert h.ir_sent == []


async def test_cancel_word_cancels(harness):
    h = harness
    seed_appliances(h)
    await _wake_and_command(h)
    await h.say_text("取消")
    await h.settle(0.2)
    assert h.engine.state is State.STANDBY
    assert h.ir_sent == []


# ---------- 后台禁用（R7） ----------


async def test_disabled_appliance_refuses_with_announce(harness):
    h = harness
    seed_appliances(h, heater_enabled=False)  # = 后台禁用同步落库后的本地态
    await _wake_and_command(h)
    await h.settle(0.2)
    assert h.engine.state is State.STANDBY, "禁用设备不得进入执行/确认"
    assert h.ir_sent == []
    spoken = await h.wait_speech(lambda t: "禁用" in t)
    assert pb.DEVICE_DISABLED.replace("。", "") in spoken.replace("。", ""), spoken


# ---------- 真实语音链路（KWS/ASR 就绪时）：wav 注入确认词 ----------


@pytest.mark.skipif(
    not Path(__file__).with_name("audio_fixtures").joinpath("confirm.wav").is_file(),
    reason="音频夹具未生成",
)
async def test_high_risk_confirm_with_real_voice(harness):
    """唤醒/指令仍文本注入（ASR 对"打开取暖器"不可靠，见偏差记录），
    确认词走真实 wav → LISTEN 路由 ASR → endpoint 切句。"""
    h = harness
    seed_appliances(h)
    await _wake_and_command(h)
    assert h.engine.state is State.CONFIRMING
    if h.asr_stream is None:
        await h.say_text("确认")  # 无模型环境降级文本注入
    else:
        await h.say_wav("confirm", tail_seconds=2.4)  # ASR 切句需 ≥2s 尾静音
    await h.settle(0.3)
    assert ("取暖器", "power") in h.ir_sent
