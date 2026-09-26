"""AE2：拍摄药品说明书的三种网络/内容形态（R3/R27/R4/R26）。

- 联网 + 医疗文本：远程转白话朗读，末尾必附"以原说明书为准"。
- 断网 + 医疗文本：降级读 OCR 原文，同样附"以原说明书为准"（不静默、不拒读）。
- 简单信件（非医疗、短文本）：不经 LLM，本地直读。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import DEFAULT_LLM_REPLY  # noqa: E402

from lira.dialog import phrasebook as pb  # noqa: E402
from lira.dialog.state_machine import State  # noqa: E402

MEDICINE_TEXT = (
    "布洛芬缓释胶囊\n"
    "用法用量：口服，一次一粒，一日两次\n"
    "禁忌：对本品过敏者禁用。不良反应：偶见轻度胃肠不适。\n"
    "请按说明书或在药师指导下服用。"
)
LETTER_TEXT = "妈妈：这周日中午回家吃饭，给你留了红烧肉。天冷记得添衣服。女儿"


async def _start_reading(h, ocr_text: str):
    h.ocr.text = ocr_text
    await h.engine.on_wake()
    await h.say_text("帮我读一下这个")
    state = await h.wait_state(State.READING)
    await h.settle(0.5)
    return state


# ---------- 联网 + 医疗 → 转白话 + 免责 ----------


async def test_online_medical_read_colloquial_with_disclaimer(harness):
    h = harness
    state = await _start_reading(h, MEDICINE_TEXT)
    assert state is State.READING
    assert h.llm_fake.requests >= 1, "医疗文本必须经远程转白话"
    assert any("布洛芬" in p for p in h.llm_fake.prompts), "OCR 原文应发给远程模型"
    played = h.speaker.played
    assert played[-1].endswith(pb.DISCLAIMER_MEDICAL.strip("。")) or \
        pb.DISCLAIMER_MEDICAL in played[-1], f"末块应附免责语句: {played[-1]}"
    assert "以原说明书为准" in played[-1]
    await h.say_text("停止")


# ---------- 断网 + 医疗 → 原文 + 免责 ----------


async def test_offline_medical_read_falls_back_to_original(harness):
    h = harness
    h.llm_fake.net_ok = False  # 断网：MockTransport 抛 ConnectError
    state = await _start_reading(h, MEDICINE_TEXT)
    assert state is State.READING
    assert h.llm_fake.requests >= 1, "应尝试过远程（失败后降级）"
    played = h.speaker.played
    # 降级读 OCR 原文（逐行成块），不拒读
    for line in [ln.strip() for ln in MEDICINE_TEXT.splitlines() if ln.strip()]:
        assert any(line in blk for blk in played), f"原文行应被读出: {line}"
    assert any("以原说明书为准" in blk for blk in played), "降级也必须附同样免责"
    await h.say_text("停止")


# ---------- 简单信件 → 本地直读，零网络 ----------


async def test_simple_letter_read_locally_without_llm(harness):
    h = harness
    state = await _start_reading(h, LETTER_TEXT)
    assert state is State.READING
    assert h.llm_fake.requests == 0, "简单文本不得发起远程请求（R3 阈值/类别判定）"
    played = h.speaker.played
    for line in [ln.strip() for ln in LETTER_TEXT.splitlines() if ln.strip()]:
        assert any(line in blk for blk in played), f"信件内容应被直读: {line}"
    assert not any("以原说明书为准" in blk for blk in played), "非医疗内容不得附医疗免责"
    await h.say_text("停止")


# ---------- 长文联网转白话：默认罐头回复形态 ----------


async def test_online_long_article_goes_through_llm_reply(harness):
    h = harness
    h.llm_fake.reply = DEFAULT_LLM_REPLY  # 多块长文（供后续 AE4 复用同形态）
    state = await _start_reading(h, "x" * 200)  # 超长非医疗 → 长度阈值触发转白话
    assert state is State.READING
    assert h.llm_fake.requests == 1
    assert len(h.speaker.played) >= 2, "长回复应切成多朗读块"
    await h.say_text("停止")
