"""AE1：隐私模式开（麦关）→ 说话不响应、零网络请求；关隐私后朗读恢复正常。

- 开隐私：wav 唤醒注入被结构性丢弃（PrivacyGatedSink），KWS/ASR 零输入、
  远程 LLM 零请求（断言面 = FakeRemoteLlm.requests 与 dropped_samples）。
- 关隐私：拍摄→识别→朗读全链路正常（药品说明书整链走通；联网转白话
  属 AE2 主场，此处只断言"恢复正常朗读"）。
"""

from __future__ import annotations

from lira.dialog.state_machine import State

# ---------- AE1 前半：隐私开 → 说话不响应 + 零网络 ----------


async def test_privacy_on_blocks_wake_and_keeps_zero_network(harness):
    h = harness
    await h.privacy.set_enabled(True, source="admin")

    log_before = list(h.speak_log)
    requests_before = h.llm_fake.requests

    await h.say_wav("wake")  # 真实 KWS 音频，但隐私门在识别流之前丢弃
    await h.say_wav("open_lamp")  # 唤醒词之外的任何语音同样不得触发任何动作

    assert h.engine.state is State.STANDBY, f"隐私期不得离开待机: {h.engine.state}"
    assert h.speak_log == log_before, f"隐私期不得有任何播报: {h.speak_log}"
    assert h.llm_fake.requests == requests_before == 0, "隐私期不得发起任何网络请求"
    assert h._gate.dropped_samples > 0, "隐私门应丢弃全部音频样本（结构性关麦）"
    assert h.ir_sent == [], "隐私期不得发出红外"


async def test_privacy_on_blocks_text_wake_too(harness):
    """殊途同归（R19）：无论入口形态，隐私 ON 时 wake_allowed 一律拒绝。"""
    h = harness
    await h.privacy.set_enabled(True, source="button")
    await h.engine.on_wake()
    assert h.engine.state is State.STANDBY, "隐私期 on_wake 必须被拒"

    await h.privacy.set_enabled(False, source="admin")
    await h.engine.on_wake()
    assert h.engine.state is State.LISTENING, "关隐私后唤醒恢复"


# ---------- AE1 后半：关隐私 → 识别朗读正常 ----------


async def test_privacy_off_reading_chain_works(harness):
    h = harness
    await h.privacy.set_enabled(False, source="admin")

    await h.engine.on_wake()
    await h.say_text("帮我读一下这个")
    state = await h.wait_state(State.READING)
    assert state is State.READING

    played = list(h.speaker.played)
    assert played, "朗读必须有播报块"
    assert h.camera.capture_calls == 1, "应完成一次拍摄"
    assert h.ocr.read_calls == 1, "应完成一次 OCR 识别"
    await h.say_text("停止")
    await h.settle(0.3)
    assert h.engine.state is State.STANDBY
