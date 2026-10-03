"""OpenAI 兼容远程 LLM 客户端（U5，R2/R3/R10/R12/R27/R28）。

要点（计划 Key Technical Decisions）：
  - `openai` SDK v1+ 默认 `max_retries=2` 且自动重试超时/连接错误；设备端显式
    `max_retries=0`，把重试语义完全收归熔断器（隐藏重试会拖垮 R28 的 2 秒
    等待反馈，并污染熔断失败计数）。
  - 隐私拦截在客户端入口（fail-closed，R12/AE1）：privacy ON 时任何远端调用
    在发起网络请求前直接抛 `PrivacyBlocked`，网络层零请求；隐私状态每调用
    现查（可注入谓词），中途开关即时生效。
  - R10：`is_available()` 把联网检测（可注入 `network_ok`）与熔断状态合并为
    "远程可用性"单一信号，喂给 U3 路由层。
  - R28：流式首 token 超过 `wait_feedback_seconds` 仍未来时触发一次
    `on_wait_feedback` 回调；实际播报（phrasebook.WAIT_REMOTE）由装配层接。
  - 日志纪律（U7/R12 同源）：只记事件类型与耗时，绝不落 prompt/回复内容。

测试入口：`openai_client` 参数可注入装配了 httpx MockTransport 的
AsyncOpenAI（网络层计数断言）；`breaker` 可注入虚拟时钟的熔断器。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from lira.config import LlmConfig
from lira.dialog.phrasebook import WAIT_REMOTE
from lira.llm.breaker import CircuitBreaker
from lira.llm.prompts import (
    PLAIN_SPEAK_SYSTEM_PROMPT,
    assemble_colloquial,
    is_complex_text,
    is_medical_text,
)

__all__ = [
    "LlmClient",
    "LlmError",
    "PrivacyBlocked",
    "make_remote_handler",
    "make_text_polisher",
    "WAIT_REMOTE",  # re-export：等待反馈话术唯一出处仍是 phrasebook
]

logger = logging.getLogger(__name__)


class PrivacyBlocked(Exception):
    """隐私模式开启，远端调用在入口被拦截（R12，fail-closed）。"""


class LlmError(Exception):
    """远程调用失败（网络/超时/服务端错误）。不携带任何对话内容。"""


class LlmClient:
    """OpenAI 兼容流式客户端 + 隐私拦截 + 熔断计数（重试归熔断器）。"""

    def __init__(
        self,
        cfg: LlmConfig,
        *,
        privacy: Callable[[], bool] = lambda: False,
        network_ok: Callable[[], bool] = lambda: True,
        on_wait_feedback: Callable[[], None] | None = None,
        breaker: CircuitBreaker | None = None,
        openai_client: AsyncOpenAI | None = None,
    ) -> None:
        # 显式 max_retries=0：SDK 不得自作主张重试（Key Decisions）
        self._client = openai_client or AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout_seconds,
            max_retries=0,
        )
        self._model = cfg.model
        self._privacy = privacy
        self._network_ok = network_ok
        self._on_wait_feedback = on_wait_feedback
        self._wait_feedback_seconds = cfg.wait_feedback_seconds
        self._colloquial_threshold_chars = cfg.colloquial_threshold_chars
        self._breaker = breaker or CircuitBreaker(
            failure_threshold=cfg.breaker_failure_threshold,
            cooldown_seconds=cfg.breaker_cooldown_seconds,
        )

    # ---------- 远程可用性（R10 单一信号） ----------

    def is_available(self) -> bool:
        """联网检测 && 熔断未开路。路由层只看这一个信号。"""
        return self._network_ok() and self._breaker.is_available()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    # ---------- 基础对话（流式回收） ----------

    async def chat(self, prompt: str, *, system: str | None = None) -> str:
        """发起一次流式对话并回收完整文本。

        Raises:
            PrivacyBlocked: 隐私模式开启（网络层零请求）。
            LlmError: 连接失败/超时（已计入熔断）或服务端错误。
        """
        # 隐私拦截在入口：任何网络活动之前（R12 fail-closed，AE1）
        if self._privacy():
            logger.info("llm.chat blocked_by=privacy")
            raise PrivacyBlocked("隐私模式开启，已拦截远程调用")

        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        started = time.monotonic()
        try:
            text = await self._stream_chat(messages)
        except APIConnectionError as exc:
            # 含 APITimeoutError（其父类是 APIConnectionError），由熔断器计数
            self._breaker.record_failure()
            logger.info("llm.chat event=conn_error elapsed=%.3fs", time.monotonic() - started)
            raise LlmError("远程服务连接失败") from exc
        except APIStatusError as exc:
            # 5xx 计入熔断（服务端持续故障应开断路）；4xx 属配置问题不计
            if exc.status_code >= 500:
                self._breaker.record_failure()
            logger.info("llm.chat event=http_%d elapsed=%.3fs", exc.status_code, time.monotonic() - started)
            raise LlmError("远程服务返回错误") from exc
        self._breaker.record_success()
        logger.info("llm.chat event=ok elapsed=%.3fs", time.monotonic() - started)
        return text

    async def _stream_chat(self, messages: list[dict[str, str]]) -> str:
        """建流并回收全部增量；首 token 等待超窗时触发一次 R28 回调。"""

        async def _open_and_first_chunk() -> tuple[object, object]:
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,  # type: ignore[arg-type]
                stream=True,
            )
            return stream, await anext(stream)  # type: ignore[func-returns-value]

        # create + 首 token 共用一个 R28 等待窗口；用 asyncio.wait 竞速，
        # 绝不取消在途任务（取消会破坏底层流），超窗后继续等待至完成。
        task = asyncio.ensure_future(_open_and_first_chunk())
        try:
            done, _ = await asyncio.wait({task}, timeout=self._wait_feedback_seconds)
            if not done:
                self._fire_wait_feedback()
            stream, first_chunk = await task
        except asyncio.CancelledError:
            task.cancel()
            raise

        parts: list[str] = []
        chunk = first_chunk
        while True:
            self._append_delta(parts, chunk)
            try:
                chunk = await anext(stream)  # type: ignore[arg-type]
            except StopAsyncIteration:
                break
        return "".join(parts)

    @staticmethod
    def _append_delta(parts: list[str], chunk: object) -> None:
        choices = getattr(chunk, "choices", None)
        if not choices:
            return  # 首 chunk 常为 role-only / usage chunk
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None)
        if content:
            parts.append(content)

    def _fire_wait_feedback(self) -> None:
        if self._on_wait_feedback is not None:
            logger.info("llm.chat event=wait_feedback threshold=%.2fs", self._wait_feedback_seconds)
            self._on_wait_feedback()

    # ---------- 复杂文本转白话（R3/R27） ----------

    def needs_colloquial(self, text: str) -> bool:
        """复杂文本判定：长度阈值（配置）或医疗类别词（R3）。"""
        return is_complex_text(text, self._colloquial_threshold_chars)

    async def colloquial(self, text: str, *, medical: bool | None = None) -> str:
        """转白话并按 R3/R27 拼装免责语句（医疗内容末尾附 DISCLAIMER_MEDICAL）。"""
        if medical is None:
            medical = is_medical_text(text)
        answer = await self.chat(text, system=PLAIN_SPEAK_SYSTEM_PROMPT)
        return assemble_colloquial(answer, medical=medical)


def make_remote_handler(client: LlmClient) -> Callable[[str], Awaitable[str | None]]:
    """适配 U3 路由层 `LlmHandler` 签名的薄适配器。

    约定（router.py）：返回 None = 无法处理（路由降级到 UNAVAILABLE），
    异常不外抛——降级语义归路由层，播报归状态机。
    """

    async def handler(text: str) -> str | None:
        if not client.is_available():
            return None
        try:
            return await client.chat(text)
        except PrivacyBlocked:
            # 隐私后果播报（R12）由状态机/装配层负责，路由层只见降级
            return None
        except LlmError:
            return None

    return handler

def make_text_polisher(llm: LlmClient):
    """R3/R27/AE2 阅读白话钩子：简单文本本地直读；复杂文本远程转白话；
    隐私/断网/失败 -> 原文 + 免责提示（不静默、不拒读）。

    M7 起为生产与 e2e harness 共用（原两处各持一份，降级话术漂移风险）。
    """
    from lira.dialog import phrasebook as pb
    from lira.llm.prompts import is_medical_text

    async def polish(text: str) -> str:
        if not llm.needs_colloquial(text):
            return text  # 简单信件：本地 OCR 直读，不经 LLM（AE2 第三段）
        try:
            return await llm.colloquial(text)
        except (PrivacyBlocked, LlmError):
            # 降级：读原文。医疗内容仍附"以原说明书为准"（同样的免责提示），
            # 非医疗附离线说明（R27）
            suffix = pb.DISCLAIMER_MEDICAL if is_medical_text(text) else pb.DISCLAIMER_OFFLINE
            return f"{text}\n{suffix}"

    return polish
