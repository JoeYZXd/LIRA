"""聊天层（home-console 计划批次一）：家庭成员与按人文字聊天。

模块边界（计划 Implementation Units）::

    chat/store.py    ChatStore：成员/消息 SQLite 持久化（U1，R5/R7）
    chat/service.py  ChatService：上下文组装、LLM 调用与五态降级（U2）

包门面完整导出两层的公共类型（service 依赖 store，无环；装配与测试
可统一从 lira.chat 导入，不必深入子模块）。
"""

from __future__ import annotations

from lira.chat.service import ChatSendResult, ChatService
from lira.chat.store import ChatStore, DuplicateMemberError

__all__ = [
    "ChatStore",
    "DuplicateMemberError",
    "ChatService",
    "ChatSendResult",
]
