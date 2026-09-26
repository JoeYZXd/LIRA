"""远程 LLM 的提示词与结果拼装（U5，R3/R27）。

纪律：
  - 免责文案唯一出处是 `lira.dialog.phrasebook`（R3/R27），本模块只 import，
    不复制——话术单一出处。
  - 转白话 system prompt 的安全底线：剂量/数值/禁忌等信息原样保留、不得编造
    原文没有的内容、不得自行添加建议——转述失真对药品说明书是安全问题。
"""

from __future__ import annotations

from lira.dialog.phrasebook import DISCLAIMER_MEDICAL

__all__ = [
    "PLAIN_SPEAK_SYSTEM_PROMPT",
    "MEDICAL_KEYWORDS",
    "is_medical_text",
    "is_complex_text",
    "assemble_colloquial",
]

# 转白话 system prompt：面向老年人（短句、口语、无术语），信息不失真
PLAIN_SPEAK_SYSTEM_PROMPT = (
    "你是一位念给老年人听的朗读助手。请把用户发来的文本改写成通俗易懂的大白话，"
    "让没有专业知识的老人一听就明白。要求："
    "1. 用短句和日常口语，不用专业术语，必须用到的术语要顺手解释；"
    "2. 不遗漏原文的关键信息，尤其剂量、时间、数值、禁忌、警告事项必须原样说清，"
    "数字和单位不得改动；"
    "3. 绝不编造原文没有的内容，也不添加你自己的建议或判断；"
    "4. 直接说出改写后的内容，不要任何开场白和解释。"
)

# 医疗类别提示词（R3/R27：药品说明书等医疗内容一律按"复杂+需免责"处理）
MEDICAL_KEYWORDS: tuple[str, ...] = (
    "药品",
    "药物",
    "说明书",
    "用法用量",
    "剂量",
    "服用",
    "口服",
    "注射",
    "禁忌",
    "不良反应",
    "副作用",
    "处方",
    "医嘱",
    "诊断",
    "症状",
    "适应症",
)


def is_medical_text(text: str) -> bool:
    """医疗类别判定：命中任一类别词即视为医疗内容（R3 免责义务触发）。"""
    return any(keyword in text for keyword in MEDICAL_KEYWORDS)


def is_complex_text(text: str, threshold_chars: int) -> bool:
    """复杂文本判定（U5 Approach）：长度达到阈值，或属于医疗类别。

    医疗内容不受长度阈值限制——一句"每日剂量不得超过 X"也必须经转白话
    并附免责，防止老人误读。
    """
    if len(text.strip()) >= threshold_chars:
        return True
    return is_medical_text(text)


def assemble_colloquial(answer: str, *, medical: bool) -> str:
    """转白话结果拼装：医疗内容在末尾追加免责语句（R3/R27，顺序固定）。

    DISCLAIMER_MEDICAL 从 phrasebook import（单一出处），不复制文案。
    """
    answer = answer.strip()
    if not medical:
        return answer
    # 保证免责语句独立成句：结果没以句末标点收尾时补句号
    if not answer.endswith(("。", "！", "？", "；")):
        answer += "。"
    return answer + DISCLAIMER_MEDICAL
