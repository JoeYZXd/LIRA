"""聊天服务（home-console 计划 U2，R6/R8/R9/R10）。

组装上下文、调用 LLM、五态降级与日志纪律的唯一业务层：

  - `send(member_id, text)` 唯一入口，返回结构化 `ChatSendResult`；
    用户可见文案一律取自 `lira.dialog.phrasebook`（单一出处，CHAT_* 族）。
  - 聊天绕过 Router 直调 LlmClient（非图方便）：Router Tier-1 命中家电意图
    即执行（文字通道触发家电直接违反 R9），且语音适配器把降级折叠为单一
    None，聊天需要独立的多态降级语义。调用约束两条铁律按传参落实：
    `on_wait_feedback=None`（聊天永不触发设备发声）、`voice_log=False`
    （回复预览与"交互中"条目不进 VOICE_LOG）、独立超时（约 30s）。
  - 护栏（均内存实现、重启重置，家庭规模接受）：设备级全局单飞
    （同时最多 1 个在途聊天 LLM 调用，镜像 ui/dev.py 的 dev_mutex 先例，
    任一成员在途期间所有二次发送得 busy）+ 每成员固定窗口限流。
  - 失败不落库：LlmError/超时轮 user+assistant 均不落库，重试干净无重复；
    隐私拦截轮例外——单独落 user 消息（R7"自己打的字仍保存"，计入 500 上限），
    滑窗组装跳过未配对尾部，隐私关闭后下一轮正常配对（隐私期重试留重复
    文本，接受，计划评审裁决）。
  - 滑窗双界：最近 8 对 + 4000 字符预算（沿用 dialog/context.py
    ConversationHistory 默认），库是唯一事实源、从 store 组装；
    task 字段恒为 None——阅读任务事实绝不进入聊天调用。
  - 内容纪律机械保证：日志与异常只携带事件/字数/耗时/ID，
    消息文本绝不拼进异常字符串、错误响应或日志（R10）。

事件日志（R10，source=web-chat）::

    chat event=<name> source=web-chat member_id=N chars=N elapsed=S
    name ∈ send|ok|fail|rate_limited|busy|unavailable|privacy|member_gone
         | save_failed|reject

线程模型：ChatStore 无内部锁，本类的全局互斥锁保证同一事件循环内
串行访问（聊天路由全部 async def，不落线程池）。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from lira.chat.store import ChatStore
from lira.dialog.context import ContextSnapshot, ConversationPair
from lira.dialog.phrasebook import (
    CHAT_BUSY,
    CHAT_EMPTY,
    CHAT_FAILED,
    CHAT_MEMBER_GONE,
    CHAT_PRIVACY,
    CHAT_TOO_LONG,
    CHAT_UNAVAILABLE,
)
from lira.llm.client import LlmClient, LlmError, PrivacyBlocked
from lira.llm.prompts import CHAT_SYSTEM_PROMPT

__all__ = [
    "ChatService",
    "ChatSendResult",
    "MAX_MESSAGE_CHARS",
    "WINDOW_PAIRS",
    "WINDOW_MAX_CHARS",
    "CHAT_TIMEOUT_SECONDS",
]

logger = logging.getLogger(__name__)

#: 单条消息长度上限（字），超限显式拒绝（计划 Key Decisions 边界数值）
MAX_MESSAGE_CHARS = 2000
#: 上下文窗口对数上界（计划 Key Decisions：最近 8 对）
WINDOW_PAIRS = 8
#: 上下文窗口字符预算（沿 dialog/context.py ConversationHistory 默认）
WINDOW_MAX_CHARS = 4000
#: 聊天调用独立超时（秒）：共享客户端默认 10s 按语音回复长度调校，长回复
#: 中途卡顿即计一次熔断失败；聊天放宽但不改语音行为（默认不变的可选参数）
CHAT_TIMEOUT_SECONDS = 30.0
#: 每成员固定窗口限流默认值（条 / 窗口秒；纯内存，重启重置）
RATE_LIMIT_MAX = 6
RATE_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class ChatSendResult:
    """send() 的结构化结果：降级态 + 用户可见文案 + 回复与落库标记。

    state: ok | privacy | unavailable | busy | failed | member_gone |
           too_long | empty
    message: 用户可见降级文案（phrasebook CHAT_* 单一出处；ok 态为空串，
        回复即内容）；一律不含消息文本（R10）。
    reply: 仅 ok 态非 None；落库失败时回复仍返回（saved=False 标记，非静默）。
    saved: 本轮是否有内容落库——ok 态 user+assistant 成对；privacy 态仅
        user 消息（R7）；failed/busy/unavailable/校验拒绝轮与落库失败为 False。
    """

    state: str
    message: str = ""
    reply: str | None = None
    saved: bool = False


class ChatService:
    """按人文字聊天业务层。依赖全部注入，可全面单测。

    `now` 可注入（沿 chat/store.py 的 clock 先例，默认 time.monotonic），
    供限流窗口测试推时；`rate_limit`/`rate_window_seconds` 为限流参数。
    """

    def __init__(
        self,
        store: ChatStore,
        llm: LlmClient,
        *,
        privacy: Callable[[], bool],
        remote_available: Callable[[], bool],
        now: Callable[[], float] = time.monotonic,
        rate_limit: int = RATE_LIMIT_MAX,
        rate_window_seconds: float = RATE_WINDOW_SECONDS,
    ) -> None:
        self._store = store
        self._llm = llm
        self._privacy = privacy
        self._remote_available = remote_available
        self._now = now
        self._rate_limit = rate_limit
        self._rate_window_seconds = rate_window_seconds
        #: 设备级全局单飞锁：同时最多 1 个在途聊天调用（窗口组装 + LLM + 落库）
        self._mutex = asyncio.Lock()
        #: 每成员固定窗口限流：member_id -> (窗口桶序号, 窗口内已计数)
        self._rate_counts: dict[int, tuple[int, int]] = {}

    # ---------- 依赖访问面 ----------

    @property
    def store(self) -> ChatStore:
        """成员/历史库（home-console U3 的 UI 成员管理路由使用；只暴露注入的库）。"""
        return self._store

    # ---------- 唯一入口 ----------

    async def send(self, member_id: int, text: str) -> ChatSendResult:
        """发一条消息并等待完整回复。

        校验/护栏未过不调 LLM 不落库；LlmError 轮不落库（重试干净）；
        隐私拦截轮单独落 user 消息（R7）。
        """
        # 成员存在性先行：写事务前的早期复查（发送中被删由落库 FK 兜底）
        if self._store.get_member(member_id) is None:
            logger.info(
                "chat event=member_gone source=web-chat member_id=%d chars=%d",
                member_id, len(text),
            )
            return ChatSendResult(state="member_gone", message=CHAT_MEMBER_GONE)

        message = text.strip()
        if not message:
            logger.info(
                "chat event=reject reason=empty source=web-chat member_id=%d", member_id
            )
            return ChatSendResult(state="empty", message=CHAT_EMPTY)
        if len(message) > MAX_MESSAGE_CHARS:
            logger.info(
                "chat event=reject reason=too_long source=web-chat member_id=%d chars=%d",
                member_id, len(message),
            )
            return ChatSendResult(state="too_long", message=CHAT_TOO_LONG)

        # 全局单飞（镜像 dev_mutex：locked() 显式拒绝后加锁，锁内无 await 间隙）
        if self._mutex.locked():
            logger.info(
                "chat event=busy source=web-chat member_id=%d chars=%d",
                member_id, len(message),
            )
            return ChatSendResult(state="busy", message=CHAT_BUSY)

        async with self._mutex:
            started = self._now()
            logger.info(
                "chat event=send source=web-chat member_id=%d chars=%d",
                member_id, len(message),
            )
            if not self._rate_limit_allow(member_id):
                logger.info(
                    "chat event=rate_limited source=web-chat member_id=%d chars=%d",
                    member_id, len(message),
                )
                return ChatSendResult(state="busy", message=CHAT_BUSY)
            if self._privacy():
                # R8：隐私 ON 一律降级，不发起任何 LLM 调用
                return self._persist_privacy_turn(member_id, message, started)
            if not self._remote_available():
                logger.info(
                    "chat event=unavailable source=web-chat member_id=%d chars=%d",
                    member_id, len(message),
                )
                return ChatSendResult(state="unavailable", message=CHAT_UNAVAILABLE)
            return await self._call_llm_and_persist(member_id, message, started)

    # ---------- 内部流程 ----------

    def _persist_privacy_turn(self, member_id: int, message: str, started: float) -> ChatSendResult:
        """隐私拦截轮：不调 LLM，user 消息单独落库（R7"自己打的字仍保存"）。

        无 assistant 配对、计入 500 条上限；滑窗组装跳过该未配对尾部。
        """
        try:
            self._store.append_messages(member_id, [("user", message)])
        except sqlite3.IntegrityError:
            # 隐私轮落库撞上"发送中成员被删"（FK）：按 member_gone 呈现
            logger.warning(
                "chat event=member_gone source=web-chat member_id=%d chars=%d elapsed=%.3fs",
                member_id, len(message), self._now() - started,
            )
            return ChatSendResult(state="member_gone", message=CHAT_MEMBER_GONE)
        except sqlite3.Error:
            # 落库失败不掩盖降级原因：仍按 privacy 呈现，仅额外记事件（非静默）
            logger.warning(
                "chat event=save_failed source=web-chat member_id=%d chars=%d elapsed=%.3fs",
                member_id, len(message), self._now() - started,
            )
            return ChatSendResult(state="privacy", message=CHAT_PRIVACY, saved=False)
        logger.info(
            "chat event=privacy source=web-chat member_id=%d chars=%d elapsed=%.3fs",
            member_id, len(message), self._now() - started,
        )
        return ChatSendResult(state="privacy", message=CHAT_PRIVACY, saved=True)

    async def _call_llm_and_persist(self, member_id: int, message: str, started: float) -> ChatSendResult:
        window = self._build_window(member_id)
        # task 恒为 None（默认值）：阅读任务事实绝不进入聊天调用
        snapshot = ContextSnapshot(pairs=tuple(ConversationPair(u, a) for u, a in window))
        try:
            reply = await self._llm.chat(
                message,
                system=CHAT_SYSTEM_PROMPT,
                context=snapshot,
                on_wait_feedback=None,  # 铁律：聊天永不触发设备发声
                timeout_seconds=CHAT_TIMEOUT_SECONDS,
                voice_log=False,  # 回复预览与"交互中"条目不进语音展示面
            )
        except PrivacyBlocked:
            # 服务端检查与客户端入口之间的隐私开关竞态：客户端 fail-closed
            # 拦截（零网络），同样按隐私轮处理（user 消息仍落库，R7）
            return self._persist_privacy_turn(member_id, message, started)
        except LlmError:
            # 失败不落库：user+assistant 均不落，重试干净无重复（Key Decisions）
            logger.info(
                "chat event=fail source=web-chat member_id=%d chars=%d elapsed=%.3fs",
                member_id, len(message), self._now() - started,
            )
            return ChatSendResult(state="failed", message=CHAT_FAILED)
        try:
            # 单事务成对落库 + 裁剪（store 内部完成）
            self._store.append_messages(member_id, [("user", message), ("assistant", reply)])
        except sqlite3.IntegrityError:
            # 写事务 FK 复查兜底"发送中成员被删"竞态（store 单事务已整体回滚）
            logger.warning(
                "chat event=member_gone source=web-chat member_id=%d chars=%d elapsed=%.3fs",
                member_id, len(message), self._now() - started,
            )
            return ChatSendResult(state="member_gone", message=CHAT_MEMBER_GONE)
        except sqlite3.Error:
            # 落库失败不吞回复：返回 + saved=False 标记 + 事件日志（非静默、非 500）
            logger.warning(
                "chat event=save_failed source=web-chat member_id=%d chars=%d elapsed=%.3fs",
                member_id, len(message), self._now() - started,
            )
            return ChatSendResult(state="ok", reply=reply, saved=False)
        logger.info(
            "chat event=ok source=web-chat member_id=%d chars=%d elapsed=%.3fs",
            member_id, len(message), self._now() - started,
        )
        return ChatSendResult(state="ok", reply=reply, saved=True)

    def _build_window(self, member_id: int) -> list[tuple[str, str]]:
        """从库组装滑窗：最近 WINDOW_PAIRS 对 + WINDOW_MAX_CHARS 字符预算双界。

        - 跳过未配对的尾部 user 消息（隐私拦截轮遗留，绝不作为上下文回传）；
        - 超限丢弃语义镜像 ConversationHistory：最新一对单独超预算则本轮窗口
          为空（不带过期上下文）；否则自最旧端裁剪至预算内；
        - 多取一行（+1）用于识别未配对尾部后仍凑满 8 对。
        """
        rows = self._store.recent_messages(member_id, limit=WINDOW_PAIRS * 2 + 1)
        if rows and rows[-1]["role"] == "user":
            rows = rows[:-1]
        newest_first: list[tuple[str, str]] = []
        i = len(rows)
        while i >= 2:
            user_row, assistant_row = rows[i - 2], rows[i - 1]
            if user_row["role"] != "user" or assistant_row["role"] != "assistant":
                break  # 中段不成对（防御性）：更旧历史一并放弃，保上下文连续
            newest_first.append((user_row["content"], assistant_row["content"]))
            i -= 2
        newest_first = newest_first[:WINDOW_PAIRS]
        kept: list[tuple[str, str]] = []
        total = 0
        for pair in newest_first:
            chars = len(pair[0]) + len(pair[1])
            if chars > WINDOW_MAX_CHARS:
                break  # 该对超预算：它与更旧历史一并丢弃（镜像 clear 语义）
            kept.append(pair)
            total += chars
        while kept and total > WINDOW_MAX_CHARS:
            dropped = kept.pop(0)  # 自最旧端裁剪（镜像 popleft 语义）
            total -= len(dropped[0]) + len(dropped[1])
        kept.reverse()  # 旧→新，供 ContextSnapshot 顺序拼接
        return kept

    def _rate_limit_allow(self, member_id: int) -> bool:
        """每成员固定窗口限流：窗口内计数达上限即拒绝；换窗重新计数。"""
        bucket = int(self._now() // self._rate_window_seconds)
        count, seen = self._rate_counts.get(member_id, (0, -1))
        if seen != bucket:
            count = 0  # 新窗口：重新计数（固定窗口语义，重启亦重置）
        allowed = count < self._rate_limit
        self._rate_counts[member_id] = (count + 1 if allowed else count, bucket)
        return allowed
