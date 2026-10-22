"""Cross-layer voice ownership and conversation behavior through the real harness."""
import asyncio

from lira.appliances.models import ApplianceModel
from lira.dialog.state_machine import State


async def test_queued_appliance_command_is_cancelled_by_next_wake(harness):
    h = harness
    h.store.upsert_appliance(ApplianceModel(
        name="台灯", aliases=("台灯",), actions={"power": ("打开",)},
        codes={"power": "P:300,400"}, is_high_risk=False, enabled=True,
    ))
    await h.engine.on_wake()
    await h.engine.on_asr_text("打开台灯")
    await h.engine.on_wake()
    await h.settle(0.05)
    assert h.ir_sent == [] and h.ir.sent_codes == []
    assert h.engine.state is State.LISTENING


async def test_privacy_closes_active_conversation_and_keeps_remote_silent(harness):
    h = harness
    await h.engine.on_wake()
    before = h.llm_fake.requests
    await h.privacy.set_enabled(True, source="test")
    assert h.engine.state is State.STANDBY
    await h.say_text("为什么")
    await asyncio.sleep(0)
    assert h.llm_fake.requests == before


async def test_followup_uses_played_turn_and_ending_chat_clears_it(harness):
    h = harness
    h.llm_fake.reply = "因为白天能看见太阳。"
    await h.engine.on_wake()
    await h.say_text("为什么白天是亮的")
    assert h.engine.state is State.FOLLOWUP
    await h.say_text("那晚上呢")
    turns = [m for m in h.llm_fake.message_batches[-1] if m["role"] != "system"]
    assert turns == [
        {"role": "user", "content": "为什么白天是亮的"},
        {"role": "assistant", "content": "因为白天能看见太阳。"},
        {"role": "user", "content": "那晚上呢"},
    ]
    await h.say_text("结束聊天")
    await h.engine.on_wake()
    await h.say_text("讲一个笑话")
    turns = [m for m in h.llm_fake.message_batches[-1] if m["role"] != "system"]
    assert turns == [{"role": "user", "content": "讲一个笑话"}]


async def test_book_fragment_neither_captures_nor_calls_remote(harness):
    h = harness
    await h.engine.on_wake()
    await h.say_text("这本书")
    assert h.camera.capture_calls == 0
    assert h.llm_fake.requests == 0
    assert any("读" in speech and "吗" in speech for speech in h.speak_log)
