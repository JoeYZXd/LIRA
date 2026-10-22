"""聊天服务（home-console 计划 U2，R6/R8/R9/R10）。

组装上下文、调用 LLM、五态降级与日志纪律的唯一业务层：

  - `send(member_id, text)` 唯一入口，返回结构化 `ChatSendResult`；
    用户可见文案一律取自 `lira.dialog.phrasebook`（单一出处，CHAT_* 族）。
  - 聊天绕过 Router 直调 LlmClient（非图方便）：Router Tier-1 命中家电意图
    即执行（文字通道触发家电直接违反 R9），且语音适配器把降级折叠为单一
    None，聊天需要独立的多态降级语义。调用约束两条铁律按传参落实：
    `on_wait_feedback=None`（聊天永不触发设备发声）、`voice_log=False`
    （回复预览与"交互中"条目不进 VOICE_LOG）、独立超时（单读约 30s + 总预算
    120s 防慢生成占锁）。
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
from lira.dialog.context import ContextSnapshot, ConversationHistory, ConversationPair
from lira.dialog.phrasebook import (
    CHAT_BUSY,
    CHAT_EMPTY,
    CHAT_FAILED,
    CHAT_MEMBER_GONE,
    CHAT_PRIVACY,
    CHAT_SAVE_FAILED_NOTE,
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
    "CHAT_TOTAL_TIMEOUT_SECONDS",
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
#: 单次发送总预算（秒）：上面是 httpx 单读超时，只约束单个读操作——慢生成/
#: 分段读叠加可远超单读上限，无限占住设备级全局单飞锁（所有成员都得到
#: busy）。wait_for 到点取消整个调用（_stream_chat 的 finally 负责关流），
#: 按 failed 收敛（不落库、锁随 async with 退出释放）。
CHAT_TOTAL_TIMEOUT_SECONDS = 120.0
#: 每成员固定窗口限流默认值（条 / 窗口秒；纯内存，重启重置）
RATE_LIMIT_MAX = 6
RATE_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class ChatSendResult:
    """send() 的结构化结果：降级态 + 用户可见文案 + 回复与落库标记。

    state: ok | privacy | unavailable | busy | failed | member_gone |
           too_long | empty
    message: 用户可见降级文案（phrasebook CHAT_* 单一出处；ok 态落库成功
        为空串（回复即内容），落库失败为 CHAT_SAVE_FAILED_NOTE 常驻标注）；
        一律不含消息文本（R10）。
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
        #: 每成员固定窗口限流：member_id -> (窗口内已计数, 窗口桶序号)
        self._rate_counts: dict[int, tuple[int, int]] = {}

    # ---------- 依赖访问面 ----------

    @property
    def store(self) -> ChatStore:
        """成员/历史库（home-console U3 的 UI 成员管理路由使用；只暴露注入的库）。"""
        return self._store

    # ---------- 唯一入口 ----------

    async def send(self, member_id: int, text: str) -> ChatSendResult:
        """发一条消息并等待完整回复。

        校验/护栏未过不调 LLM 不落库；LlmError/总预算超时轮不落库（重试干净）；
        隐私拦截轮单独落 user 消息（R7）。
        """
        # 成员存在性先行：写事务前的早期复查（发送中被删由落库 FK 兜底）；
        # 读失败显式降级（与写路径的 save_failed 对称，不 500、不落库）
        try:
            member_missing = self._store.get_member(member_id) is None
        except sqlite3.Error:
            self._log(
                "unavailable", member_id, len(text), reason="store_read", level=logging.WARNING
            )
            return ChatSendResult(state="unavailable", message=CHAT_UNAVAILABLE)
        if member_missing:
            self._log("member_gone", member_id, len(text))
            return ChatSendResult(state="member_gone", message=CHAT_MEMBER_GONE)

        message = text.strip()
        if not message:
            self._log("reject", member_id, reason="empty")
            return ChatSendResult(state="empty", message=CHAT_EMPTY)
        if len(message) > MAX_MESSAGE_CHARS:
            self._log("reject", member_id, len(message), reason="too_long")
            return ChatSendResult(state="too_long", message=CHAT_TOO_LONG)

        # 全局单飞（镜像 dev_mutex：locked() 显式拒绝后加锁，锁内无 await 间隙）
        if self._mutex.locked():
            self._log("busy", member_id, len(message))
            return ChatSendResult(state="busy", message=CHAT_BUSY)

        async with self._mutex:
            started = self._now()
            self._log("send", member_id, len(message))
            if not self._rate_limit_allow(member_id):
                self._log("rate_limited", member_id, len(message))
                return ChatSendResult(state="busy", message=CHAT_BUSY)
            if self._privacy():
                # R8：隐私 ON 一律降级，不发起任何 LLM 调用
                return self._persist_privacy_turn(member_id, message, started)
            if not self._remote_available():
                self._log("unavailable", member_id, len(message))
                return ChatSendResult(state="unavailable", message=CHAT_UNAVAILABLE)
            return await self._call_llm_and_persist(member_id, message, started)

    # ---------- 内部流程 ----------

    def _log(
        self,
        event: str,
        member_id: int,
        chars: int | None = None,
        started: float | None = None,
        *,
        reason: str | None = None,
        level: int = logging.INFO,
    ) -> None:
        """R10 结构化事件日志的字节级单一出处：只含事件/原因/字数/耗时/ID。

        `started` 给出时追加 elapsed（仅落库/调用完成路径）；消息文本绝不
        进入日志（内容纪律由全部调用点经由本 helper 机械保证）。
        """
        parts = ["chat event=", event]
        if reason is not None:
            parts += [" reason=", reason]
        parts += [" source=web-chat member_id=", str(member_id)]
        if chars is not None:
            parts += [" chars=", str(chars)]
        if started is not None:
            parts += [" elapsed=%.3fs" % (self._now() - started)]
        logger.log(level, "".join(parts))

    def _persist(
        self,
        member_id: int,
        rows: list[tuple[str, str]],
        started: float,
        success_result: Callable[[bool], ChatSendResult],
    ) -> ChatSendResult:
        """落库三态收敛（privacy 轮单条 / ok 轮成对共用）：

        - IntegrityError：写事务 FK 兜底"发送中成员被删"竞态（store 单事务
          已整体回滚）→ member_gone；
        - sqlite3.Error：落库失败不掩盖降级原因/不吞回复——事件日志 +
          success_result(False)（saved=False 非静默标记）；
        - 成功：success_result(True)，事件名取结果态（privacy/ok 与事件
          名一一对应）。
        """
        try:
            self._store.append_messages(member_id, rows)
        except sqlite3.IntegrityError:
            self._log("member_gone", member_id, len(rows[0][1]), started, level=logging.WARNING)
            return ChatSendResult(state="member_gone", message=CHAT_MEMBER_GONE)
        except sqlite3.Error:
            self._log("save_failed", member_id, len(rows[0][1]), started, level=logging.WARNING)
            return success_result(False)
        result = success_result(True)
        self._log(result.state, member_id, len(rows[0][1]), started)
        return result

    def _persist_privacy_turn(self, member_id: int, message: str, started: float) -> ChatSendResult:
        """隐私拦截轮：不调 LLM，user 消息单独落库（R7"自己打的字仍保存"）。

        无 assistant 配对、计入 500 条上限；滑窗组装跳过该未配对尾部。
        """
        return self._persist(
            member_id,
            [("user", message)],
            started,
            lambda saved: ChatSendResult(state="privacy", message=CHAT_PRIVACY, saved=saved),
        )

    async def _call_llm_and_persist(self, member_id: int, message: str, started: float) -> ChatSendResult:
        try:
            window = self._build_window(member_id)
        except sqlite3.Error:
            # 滑窗读失败：与写路径 save_failed 对称，显式降级（不调 LLM、不落库）
            self._log(
                "unavailable", member_id, len(message), started,
                reason="store_read", level=logging.WARNING,
            )
            return ChatSendResult(state="unavailable", message=CHAT_UNAVAILABLE)
        # task 恒为 None（默认值）：阅读任务事实绝不进入聊天调用
        snapshot = ContextSnapshot(pairs=tuple(ConversationPair(u, a) for u, a in window))
        try:
            # 总预算兜底（CHAT_TOTAL_TIMEOUT_SECONDS）：单读超时只约束单个读
            # 操作，wait_for 到点取消整个调用并释放全局单飞锁
            reply = await asyncio.wait_for(
                self._llm.chat(
                    message,
                    system=CHAT_SYSTEM_PROMPT,
                    context=snapshot,
                    on_wait_feedback=None,  # 铁律：聊天永不触发设备发声
                    timeout_seconds=CHAT_TIMEOUT_SECONDS,
                    voice_log=False,  # 回复预览与"交互中"条目不进语音展示面
                    **({"memory_member_id": member_id} if getattr(self._llm,"memory",None) is not None else {}),
                ),
                timeout=CHAT_TOTAL_TIMEOUT_SECONDS,
            )
        except PrivacyBlocked:
            # 服务端检查与客户端入口之间的隐私开关竞态：客户端 fail-closed
            # 拦截（零网络），同样按隐私轮处理（user 消息仍落库，R7）
            return self._persist_privacy_turn(member_id, message, started)
        except LlmError:
            # 失败不落库：user+assistant 均不落，重试干净无重复（Key Decisions）
            self._log("fail", member_id, len(message), started)
            return ChatSendResult(state="failed", message=CHAT_FAILED)
        except asyncio.TimeoutError:
            # 总预算超时同 LlmError 收敛：失败不落库（重试干净），锁随
            # async with 退出释放（慢生成不得无限占锁）
            self._log("fail", member_id, len(message), started)
            return ChatSendResult(state="failed", message=CHAT_FAILED)
        # 单事务成对落库 + 裁剪（store 内部完成）；落库失败不吞回复（非静默、
        # 非 500）——saved=False 时携带显式标注（前端常驻呈现，R1 残留修复）
        outcome = self._persist(
            member_id,
            [("user", message), ("assistant", reply)],
            started,
            lambda saved: ChatSendResult(
                state="ok",
                reply=reply,
                saved=saved,
                message="" if saved else CHAT_SAVE_FAILED_NOTE,
            ),
        )
        memory=getattr(self._llm,"memory",None)
        if outcome.saved and memory is not None:
            try:memory.completed(member_id,message,reply)
            except Exception:logger.warning('chat event=memory_record_failed')
        return outcome

    def _build_window(self, member_id: int) -> list[tuple[str, str]]:
        """从库组装滑窗：最近 WINDOW_PAIRS 对 + WINDOW_MAX_CHARS 字符预算双界。

        本方法只负责 chat 特有的行→对转换；双界裁剪复用 ConversationHistory：
        - 跳过未配对的尾部 user 消息（隐私拦截轮遗留，绝不作为上下文回传）；
        - 中段不成对（防御性）更旧历史一并放弃，保上下文连续；
        - 最新一对单独超预算则本轮窗口为空（不带过期上下文）；超限自最旧
          端裁剪（pop 最旧，record_completed 语义）。
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
        history = ConversationHistory(max_pairs=WINDOW_PAIRS, max_chars=WINDOW_MAX_CHARS)
        for user, assistant in reversed(newest_first):  # 旧→新喂入，供顺序裁剪
            history.record_completed(user, assistant)
        return [(pair.user, pair.assistant) for pair in history.snapshot().pairs]

    def _rate_limit_allow(self, member_id: int) -> bool:
        """每成员固定窗口限流：窗口内计数达上限即拒绝；换窗重新计数。"""
        bucket = int(self._now() // self._rate_window_seconds)
        count, seen = self._rate_counts.get(member_id, (0, -1))
        if seen != bucket:
            count = 0  # 新窗口：重新计数（固定窗口语义，重启亦重置）
        allowed = count < self._rate_limit
        self._rate_counts[member_id] = (count + 1 if allowed else count, bucket)
        return allowed
