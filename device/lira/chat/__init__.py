"""聊天层（home-console 计划批次一）：家庭成员与按人文字聊天。

模块边界（计划 Implementation Units）::

    chat/store.py    ChatStore：成员/消息 SQLite 持久化（U1，R5/R7）
    chat/service.py  ChatService：上下文组装、LLM 调用与五态降级（U2）
"""

from __future__ import annotations

from lira.chat.store import ChatStore, DuplicateMemberError

__all__ = [
    "ChatStore",
    "DuplicateMemberError",
]
