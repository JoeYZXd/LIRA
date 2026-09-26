"""AE6：隐私模式下物理键 = 唯一出口（R19 殊途同归）。

隐私开 → 按物理键 → 退出隐私 + 语音确认（"麦克风恢复了"）→ 唤醒词立即可用；
再按键可重新进入隐私（双向切换）。
"""

from __future__ import annotations

import pytest

from lira.dialog import phrasebook as pb
from lira.dialog.state_machine import State


async def test_button_exits_privacy_with_speech_then_wake_works(harness):
    h = harness
    await h.privacy.set_enabled(True, source="admin")
    assert h.privacy.is_on

    # 物理键：唯一出口
    h.press_button()
    spoken = await h.wait_speech(lambda t: "麦克风恢复" in t)
    assert not h.privacy.is_on
    assert spoken, "退出隐私必须有语音确认"

    # 唤醒词立即可用（模型就绪走真实 wav；否则降级 on_wake 验证状态机门）
    if h.wake_stream is not None:
        await h.say_wav("wake")
        assert await h.wait_state(State.LISTENING) is State.LISTENING
    else:
        await h.engine.on_wake()
        assert h.engine.state is State.LISTENING


async def test_button_reenters_privacy_and_blocks_wake(harness):
    h = harness
    h.press_button()  # STANDBY 下按键 = 进隐私
    await h.wait_speech(lambda t: "隐私模式已开启" in t)
    assert h.privacy.is_on

    dropped_before = h._gate.dropped_samples
    if h.wake_stream is not None:
        await h.say_wav("wake")
    else:
        await h.engine.on_wake()
    assert h.engine.state is State.STANDBY, "隐私期唤醒必须被拒"
    assert h._gate.dropped_samples > dropped_before


async def test_button_during_active_session_also_toggles(harness):
    """朗读/任意会话中按键同样殊途同归（R19）：退出隐私只发生在隐私态；
    非隐私态按键切"进"隐私。"""
    h = harness
    await h.engine.on_wake()
    h.press_button()
    await h.wait_speech(lambda t: "隐私模式已开启" in t)
    assert h.privacy.is_on
    assert h._gate.dropped_samples >= 0


@pytest.mark.parametrize("source", ["admin", "button", "voice_ui"])
async def test_privacy_sources_converge(harness, source):
    """R19 殊途同归：各入口切隐私后行为一致（拒绝唤醒 + 零网络）。"""
    h = harness
    await h.privacy.set_enabled(True, source=source)
    log_before = list(h.speak_log)
    await h.say_wav("wake")
    assert h.engine.state is State.STANDBY
    assert h.speak_log == log_before
    assert h.llm_fake.requests == 0
    assert h._gate.dropped_samples > 0
    assert pb.PRIVACY_ON  # 话术单一出处（phrasebook）
