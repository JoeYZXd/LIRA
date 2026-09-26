"""U5：远程 LLM 客户端、熔断降级、转白话提示词。"""

from lira.llm.breaker import BreakerState, CircuitBreaker
from lira.llm.client import LlmClient, LlmError, PrivacyBlocked, make_remote_handler
from lira.llm.prompts import (
    PLAIN_SPEAK_SYSTEM_PROMPT,
    assemble_colloquial,
    is_complex_text,
    is_medical_text,
)

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "LlmClient",
    "LlmError",
    "PrivacyBlocked",
    "make_remote_handler",
    "PLAIN_SPEAK_SYSTEM_PROMPT",
    "assemble_colloquial",
    "is_complex_text",
    "is_medical_text",
]
