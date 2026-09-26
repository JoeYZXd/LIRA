"""U5 LLM 客户端测试。

mock 远端：openai SDK `http_client` 注入 httpx2.MockTransport（openai 3.x 依赖
httpx2 分支），fake handler 统计网络层请求数——支撑 AE1 的"零请求"断言。
"""

from __future__ import annotations

import asyncio
import json

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI

from lira.config import LlmConfig
from lira.dialog.phrasebook import DISCLAIMER_MEDICAL, WAIT_REMOTE
from lira.llm import (
    LlmClient,
    LlmError,
    PrivacyBlocked,
    make_remote_handler,
)
from lira.llm.breaker import BreakerState, CircuitBreaker
from lira.llm.prompts import is_complex_text, is_medical_text

BASE_URL = "http://unit.test/v1"


# ---------- 测试基建：fake 远端 ----------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def sse_body(*deltas: str) -> bytes:
    """拼一段 OpenAI 兼容的 SSE 流式响应体。"""
    lines: list[str] = []
    base = {"id": "chatcmpl-1", "object": "chat.completion.chunk",
            "created": 1700000000, "model": "unit-model"}
    for delta in deltas:
        chunk = {**base, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    lines.append("data: " + json.dumps(final) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


SSE_HEADERS = {"content-type": "text/event-stream"}


class FakeRemote:
    """按脚本回放的远端 + 网络层请求计数。

    script 每项为 handler(request) -> httpx.Response（或直接抛异常）；
    脚本耗尽时复用最后一项。`requests` 是网络层收到的请求数。
    """

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests = 0
        self.last_request: httpx.Request | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        self.last_request = request
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return step(request)


def make_llm_cfg(**overrides) -> LlmConfig:
    values = dict(
        base_url=BASE_URL,
        api_key="sk-test",
        model="unit-model",
        timeout_seconds=10.0,
        breaker_failure_threshold=3,
        breaker_cooldown_seconds=60.0,
        wait_feedback_seconds=2.0,
        colloquial_threshold_chars=150,
    )
    values.update(overrides)
    return LlmConfig(**values)


def make_client(fake: FakeRemote, cfg: LlmConfig | None = None, **kwargs) -> LlmClient:
    transport = httpx.MockTransport(fake)
    oai = AsyncOpenAI(
        api_key="sk-test",
        base_url=BASE_URL,
        http_client=httpx.AsyncClient(transport=transport),
        max_retries=0,
    )
    return LlmClient(cfg or make_llm_cfg(), openai_client=oai, **kwargs)


def ok_response(*deltas: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=SSE_HEADERS, content=sse_body(*deltas))

    return handler


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("boom", request=request)


def read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "boom"})


# ---------- 流式文本回收（Happy path） ----------


class TestStreamingRecovery:
    async def test_stream_full_text_recovered_in_order(self):
        fake = FakeRemote(ok_response("你好", "，", "世", "界"))
        client = make_client(fake)
        assert await client.chat("打个招呼") == "你好，世界"
        assert fake.requests == 1

    async def test_request_uses_configured_model_and_stream(self):
        fake = FakeRemote(ok_response("好的"))
        client = make_client(fake, make_llm_cfg(model="unit-model"))
        await client.chat("测试")
        body = json.loads(fake.last_request.read().decode("utf-8"))
        assert body["model"] == "unit-model"
        assert body["stream"] is True
        assert body["messages"][-1] == {"role": "user", "content": "测试"}


class TestSdkRetryDisabled:
    def test_default_client_constructs_with_max_retries_zero(self):
        """Key Decisions：SDK 默认重试必须显式归零，重试语义归熔断器。"""
        client = LlmClient(make_llm_cfg())
        assert client._client.max_retries == 0

    async def test_server_error_not_retried_by_sdk(self):
        """500 时网络层只收到 1 个请求 = SDK 没有偷偷重试。"""
        fake = FakeRemote(server_error)
        client = make_client(fake)
        with pytest.raises(LlmError):
            await client.chat("测试")
        assert fake.requests == 1


# ---------- 熔断器联动（Error path: 3 次失败 → 开路 → 半开 → 闭合） ----------


class TestBreakerIntegration:
    async def test_three_connection_errors_open_circuit(self):
        fake = FakeRemote(connect_error)
        client = make_client(fake)
        for _ in range(3):
            with pytest.raises(LlmError):
                await client.chat("测试")
        assert fake.requests == 3
        assert client.is_available() is False
        assert client.breaker.state is BreakerState.OPEN

    async def test_timeout_counts_as_breaker_failure(self):
        """APITimeoutError 由熔断器统一计数（计划 Approach）。"""
        fake = FakeRemote(read_timeout)
        client = make_client(fake)
        for _ in range(3):
            with pytest.raises(LlmError):
                await client.chat("测试")
        assert client.is_available() is False

    async def test_circuit_open_blocks_requests_saving_cooldown_half_open(self):
        """开路期间不再发请求（请求计数停在 3）；冷却后半开，成功即闭合。"""
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=clock)
        fake = FakeRemote(connect_error, connect_error, connect_error, ok_response("恢复了"))
        client = make_client(fake, breaker=breaker)

        for _ in range(3):
            with pytest.raises(LlmError):
                await client.chat("测试")
        assert fake.requests == 3
        assert client.is_available() is False

        clock.advance(59.0)
        assert client.is_available() is False  # 冷却未满
        clock.advance(1.5)
        assert client.is_available() is True   # HALF_OPEN 放行试探

        assert await client.chat("测试") == "恢复了"
        assert fake.requests == 4
        assert client.breaker.state is BreakerState.CLOSED

    async def test_half_open_probe_failure_reopens(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0, clock=clock)
        fake = FakeRemote(connect_error, connect_error)
        client = make_client(fake, breaker=breaker)

        with pytest.raises(LlmError):
            await client.chat("测试")
        clock.advance(60.0)
        assert client.is_available() is True
        with pytest.raises(LlmError):
            await client.chat("测试")  # 半开试探失败
        assert client.is_available() is False
        clock.advance(30.0)
        assert client.is_available() is False  # 新冷却期重新计满才算恢复


# ---------- 隐私拦截（AE1，R12 fail-closed） ----------


class TestPrivacyGate:
    async def test_privacy_on_blocks_before_any_network_request(self):
        fake = FakeRemote(ok_response("不应到达"))
        client = make_client(fake, privacy=lambda: True)
        with pytest.raises(PrivacyBlocked):
            await client.chat("读一下说明书")
        assert fake.requests == 0, "隐私 ON 时网络层必须零请求"

    async def test_privacy_toggle_off_allows_later_calls(self):
        """隐私状态每调用现查：中途关闭后新调用放行；已拦截调用不复活。"""
        privacy_on = {"value": True}
        fake = FakeRemote(ok_response("好的"))
        client = make_client(fake, privacy=lambda: privacy_on["value"])

        with pytest.raises(PrivacyBlocked):
            await client.chat("被拦截的调用")
        assert fake.requests == 0

        privacy_on["value"] = False  # 隐私中途关闭
        assert await client.chat("新调用") == "好的"
        assert fake.requests == 1  # 被拦截的那次没有补发

    async def test_privacy_toggled_on_mid_session_blocks_next_call(self):
        privacy_on = {"value": False}
        fake = FakeRemote(ok_response("好的"), ok_response("不应到达"))
        client = make_client(fake, privacy=lambda: privacy_on["value"])
        assert await client.chat("正常调用") == "好的"
        privacy_on["value"] = True  # 隐私中途开启
        with pytest.raises(PrivacyBlocked):
            await client.chat("第二次调用")
        assert fake.requests == 1

    async def test_privacy_on_marks_unavailable(self):
        """隐私 ON 也会收敛到"远程不可用"信号（装配层可据此直接降级）。"""
        fake = FakeRemote(ok_response("x"))
        client = make_client(fake, privacy=lambda: True)
        handler = make_remote_handler(client)
        assert await handler("任意") is None
        assert fake.requests == 0


# ---------- R28 等待反馈钩子 ----------


def slow_sse_response(delay: float, *deltas: str):
    """首个字节延迟到达的流式响应（R28 场景：远端迟迟不出首 token）。"""
    body = sse_body(*deltas)

    async def gen():
        await asyncio.sleep(delay)
        yield body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=SSE_HEADERS, content=gen())

    return handler


class TestWaitFeedbackHook:
    async def test_slow_first_token_fires_callback_once(self):
        calls: list[bool] = []
        fake = FakeRemote(slow_sse_response(0.15, "你", "好"))
        client = make_client(
            fake,
            make_llm_cfg(wait_feedback_seconds=0.02),
            on_wait_feedback=lambda: calls.append(True),
        )
        text = await client.chat("测试")
        assert text == "你好"  # 慢响应不丢内容
        assert calls == [True], "首 token 超窗应触发恰好一次等待反馈回调"

    async def test_fast_first_token_does_not_fire_callback(self):
        calls: list[bool] = []
        fake = FakeRemote(ok_response("你好"))
        client = make_client(
            fake,
            make_llm_cfg(wait_feedback_seconds=2.0),
            on_wait_feedback=lambda: calls.append(True),
        )
        assert await client.chat("测试") == "你好"
        assert calls == []

    async def test_no_callback_configured_is_noop(self):
        fake = FakeRemote(slow_sse_response(0.05, "好"))
        client = make_client(fake, make_llm_cfg(wait_feedback_seconds=0.01))
        assert await client.chat("测试") == "好"  # 未装配回调也不报错

    def test_wait_phrase_single_source(self):
        """回调只是信号：播报话术唯一出处是 phrasebook.WAIT_REMOTE（R28）。"""
        assert WAIT_REMOTE == "正在为您仔细阅读，请稍等。"


# ---------- 复杂文本转白话与免责拼装（R3/R27） ----------


class TestColloquialAssembly:
    async def test_medical_text_gets_disclaimer_appended_last(self):
        fake = FakeRemote(ok_response("这个药一天吃三次", "，每次一片"))
        client = make_client(fake)
        text = "布洛芬说明书：用法用量，成人一次0.3g。"
        result = await client.colloquial(text)
        assert result == "这个药一天吃三次，每次一片" + "。" + DISCLAIMER_MEDICAL
        body = json.loads(fake.last_request.read().decode("utf-8"))
        assert body["messages"][0]["role"] == "system"  # 转白话 system prompt 生效

    async def test_non_medical_text_has_no_disclaimer(self):
        fake = FakeRemote(ok_response("大白话内容"))
        client = make_client(fake)
        result = await client.colloquial("这是一封普通的家书" * 20)
        assert result == "大白话内容"
        assert DISCLAIMER_MEDICAL not in result

    async def test_medical_flag_can_be_forced(self):
        fake = FakeRemote(ok_response("内容"))
        client = make_client(fake)
        result = await client.colloquial("无关内容", medical=True)
        assert result.endswith(DISCLAIMER_MEDICAL)


class TestComplexityDetection:
    def test_long_text_exceeding_threshold_is_complex(self):
        assert is_complex_text("字" * 151, threshold_chars=150) is True
        assert is_complex_text("字" * 100, threshold_chars=150) is False

    def test_medical_keywords_trigger_regardless_of_length(self):
        assert is_medical_text("禁忌：孕妇禁用") is True
        assert is_medical_text("今天天气不错") is False
        # 一句话的剂量警告也必须走转白话+免责
        assert is_complex_text("每日剂量不得超过2片", threshold_chars=150) is True


# ---------- U3 路由适配器 ----------


class TestRemoteHandlerAdapter:
    async def test_adapter_returns_text_when_available(self):
        fake = FakeRemote(ok_response("远程回答"))
        client = make_client(fake)
        handler = make_remote_handler(client)
        assert await handler("复杂问题") == "远程回答"

    async def test_adapter_returns_none_when_breaker_open(self):
        fake = FakeRemote(connect_error)
        client = make_client(fake)
        for _ in range(3):
            with pytest.raises(LlmError):
                await client.chat("x")
        handler = make_remote_handler(client)
        assert await handler("复杂问题") is None
        assert fake.requests == 3  # 开路期间不再发请求

    async def test_adapter_swallows_connection_error_and_counts_failure(self):
        fake = FakeRemote(connect_error, ok_response("好的"))
        client = make_client(fake)
        handler = make_remote_handler(client)
        assert await handler("复杂问题") is None  # 异常不外抛，路由层只见 None
        assert client.breaker.state is BreakerState.CLOSED  # 1 次失败未达阈值 3
        assert await handler("复杂问题") == "好的"
