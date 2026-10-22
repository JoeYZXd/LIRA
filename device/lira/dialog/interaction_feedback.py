"""Bounded local conversational cues; never an intent-routing authority."""


def waiting_cue(text: str) -> str:
    """Scan at most 256 characters; do not claim a tool has run."""
    sample = text[:256]
    if any(word in sample for word in ("为什么", "怎么", "如何", "分析", "解释", "建议")):
        return "嗯，我想想。"
    return "嗯。"
