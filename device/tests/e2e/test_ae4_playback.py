"""AE4：朗读内容与家电指令的结构性隔离（R11）+ 白名单播放控制（R21/R22）。

文章内容含"打开电视"（指令词形）也不得触发家电动作：READING 态音频路由
只喂白名单 KWS，非白名单词永远进不了意图通道；"暂停/继续/再读一遍/停止/
大声点/小声点"经白名单命中驱动播放控制。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import DEFAULT_LLM_REPLY  # noqa: E402

from lira.appliances.models import ApplianceModel  # noqa: E402
from lira.dialog.state_machine import State  # noqa: E402

#: 含指令词形的长文章（≥150 字符 → 走远程转白话，罐头回复保留"打开电视"）
ARTICLE_TEXT = "今天的电视节目单：" + "晚间打开电视看电视剧，从八点开始一共三集。" * 8


def seed_lamp(h) -> None:
    h.store.upsert_appliance(ApplianceModel(
        name="台灯", aliases=("台灯", "电灯"),
        actions={"power": ("打开",)}, codes={"power": "P:300,400"},
        is_high_risk=False, enabled=True))


async def _start_article_reading(h) -> None:
    h.llm_fake.reply = DEFAULT_LLM_REPLY  # 长回复 → 多块，给播放控制留 runway
    h.ocr.text = ARTICLE_TEXT
    await h.engine.on_wake()
    await h.say_text("帮我读一下这个")
    state = await h.wait_state(State.READING, timeout=15.0)
    assert state is State.READING


async def _wait_block_containing(h, needle: str, timeout: float = 20.0) -> None:
    """等待朗读推进到包含指定片段的块（块播放是真实节拍）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(needle in blk for blk in h.speaker.played):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"朗读未出现目标块: {needle}; played={h.speaker.played}")


# ---------- 朗读中：指令词形被结构性隔离 ----------


async def test_article_content_never_triggers_appliance(harness):
    h = harness
    seed_lamp(h)
    await _start_article_reading(h)
    await _wait_block_containing(h, "打开电视")
    assert h.ir_sent == [], "朗读内容不得触发家电"
    # 朗读进行中把含指令词形的音频灌进麦克风（PLAYBACK 路由）：非白名单
    # 词在白名单 KWS 零命中 → 无任何意图产生（先暂停，保证会话不被读完）
    await h.say_text("暂停")
    assert h.pipeline.session is not None and h.pipeline.session.is_paused
    await h.say_wav("open_lamp")
    await h.settle(0.3)
    assert h.engine.state is State.READING, "非白名单语音不得打断朗读"
    assert h.ir_sent == [], "朗读中的指令词形必须被隔离"
    await h.say_text("继续")
    await h.say_text("停止")
    await h.settle(0.3)
    assert h.engine.state is State.STANDBY
    assert h.ir_sent == []


# ---------- 白名单命令（R21/R22） ----------


async def test_whitelist_pause_resume(harness):
    h = harness
    await _start_article_reading(h)
    await h.say_text("暂停")
    assert h.pipeline.session is not None and h.pipeline.session.is_paused
    await h.settle(1.2)  # 暂停时当前块播完即停（块粒度语义）
    cursor = h.pipeline.session.cursor
    await h.settle(0.6)
    assert h.pipeline.session.cursor == cursor, "暂停期间游标不得前进"
    await h.say_text("继续")
    assert not h.pipeline.session.is_paused
    await h.say_text("停止")
    await h.settle(0.3)
    assert h.engine.state is State.STANDBY, "停止回待机（不触发自然读完信号）"


async def test_whitelist_read_again_repeats_current_block(harness):
    h = harness
    await _start_article_reading(h)
    played_before = len(h.speaker.played)
    await h.say_text("再读一遍")
    await h.settle(1.0)
    played = h.speaker.played
    assert len(played) > played_before
    assert any(a == b for a, b in zip(played, played[1:])), \
        f"再读一遍=重复当前块（出现连续重复块）: {played[-3:]}"
    assert h.camera.capture_calls == 1, "R22：绝不重新拍摄"
    await h.say_text("停止")


async def test_whitelist_volume_steps(harness):
    h = harness
    await _start_article_reading(h)
    v0 = h.speaker.volume
    await h.say_text("大声点")
    assert h.speaker.volume == pytest.approx(min(v0 + 0.2, 2.0))
    await h.say_text("小声点")
    await h.say_text("小声点")
    assert h.speaker.volume == pytest.approx(max(v0 - 0.2, 0.2))
    await h.say_text("停止")


# ---------- 真实白名单 KWS 命中（模型就绪时；否则降级文本注入已覆盖） ----------


async def test_whitelist_pause_via_real_kws(harness):
    """pause.wav（真实 TTS 语音）→ 白名单 KWS 命中 → 暂停。"""
    h = harness
    if h.playback_stream is None:
        pytest.skip("白名单 KWS 流不可用（模型未就绪，文本注入用例已覆盖逻辑）")
    await _start_article_reading(h)
    await h.say_wav("pause")
    assert h.pipeline.session is not None and h.pipeline.session.is_paused
    await h.say_wav("stop")
    await h.settle(0.3)
    assert h.engine.state is State.STANDBY
    assert h.ir_sent == []
