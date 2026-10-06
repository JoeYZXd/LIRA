"""Bounded completed voice turns and immutable per-request task facts."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypedDict


class ReadingTaskStatus(str, Enum):
    CAPTURING = "capturing"
    READING = "reading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ReadingTaskFacts:
    status: ReadingTaskStatus
    resumable: bool
    kind: Literal["reading"] = "reading"

    def as_dict(self) -> dict[str, str | bool]:
        return {"kind": self.kind, "status": self.status.value, "resumable": self.resumable}


@dataclass(frozen=True)
class ConversationPair:
    user: str
    assistant: str

    @property
    def characters(self) -> int:
        return len(self.user) + len(self.assistant)


class StatusFacts(TypedDict, total=False):
    """R17 状态自查事实（KP-02：封闭三态形状，键缺省=未知降级）。

    network：网络连通探针；cloud：LLM 熔断器推导（云端可达）；
    mic_on：隐私门状态（非麦克风硬件健康——失聪看门狗是独立未立项）。
    """

    network: bool
    cloud: bool
    mic_on: bool


@dataclass(frozen=True)
class ContextSnapshot:
    pairs: tuple[ConversationPair, ...] = ()
    task: ReadingTaskFacts | None = None

    def messages(self) -> list[dict[str, str]]:
        messages = []
        for pair in self.pairs:
            messages.extend((
                {"role": "user", "content": pair.user},
                {"role": "assistant", "content": pair.assistant},
            ))
        return messages


class ConversationHistory:
    """Call record_completed only after the matching answer played successfully."""

    def __init__(self, *, max_pairs: int = 6, max_chars: int = 4000) -> None:
        if max_pairs < 1 or max_chars < 1:
            raise ValueError("Conversation history bounds must be positive")
        self._max_pairs = max_pairs
        self._max_chars = max_chars
        self._pairs: deque[ConversationPair] = deque()
        self._characters = 0

    def record_completed(self, user: str, assistant: str) -> None:
        if not user.strip() or not assistant.strip():
            return
        pair = ConversationPair(user, assistant)
        # An oversized newest pair cannot be represented without truncating a
        # completed turn. Drop older history too, rather than expose stale context.
        if pair.characters > self._max_chars:
            self.clear()
            return
        self._pairs.append(pair)
        self._characters += pair.characters
        while len(self._pairs) > self._max_pairs or self._characters > self._max_chars:
            self._characters -= self._pairs.popleft().characters

    def snapshot(self, *, task: ReadingTaskFacts | None = None) -> ContextSnapshot:
        return ContextSnapshot(tuple(self._pairs), task)

    def last_answer(self) -> str | None:
        """最近一条成功完成的回答原文（R15「再说一遍」重报用；无则 None）。
        识别失败引导等不入史话术不在其中（只经 record_completed 写入）。"""
        return self._pairs[-1].assistant if self._pairs else None

    def clear(self) -> None:
        self._pairs.clear()
        self._characters = 0
