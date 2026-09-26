"""U3 三级意图路由测试（本地规则 → 本地 LLM → 远程 LLM）。

Phase 1：本地 LLM 层为接口 + 可配置降级（None = 未配置，直接跳过）；
远程 LLM 用注入的 fake handler + 可用性信号（U5 接真实客户端与熔断器）。
"""

from __future__ import annotations

from lira.dialog import Appliance, Router, RouterTier
from lira.dialog.intents import IntentKind

APPLIANCES = (
    Appliance(name="台灯", aliases=("台灯",), actions={"打开": ("打开",)}),
)


def make_router(local_llm=None, remote_llm=None, remote_available=False) -> Router:
    return Router(
        appliances=APPLIANCES,
        local_llm=local_llm,
        remote_llm=remote_llm,
        remote_available=lambda: remote_available,
    )


class TestTier1LocalRules:
    async def test_local_rule_hit_short_circuits(self):
        """Tier 1：本地规则命中即返回，不触达任何 LLM handler。"""
        llm_calls: list[str] = []

        async def local_llm(text: str) -> str | None:
            llm_calls.append(text)
            return "不应被调用"

        router = make_router(local_llm=local_llm)
        result = await router.route("打开台灯")
        assert result.tier is RouterTier.LOCAL_RULE
        assert result.intent is not None
        assert result.intent.kind is IntentKind.APPLIANCE
        assert result.reply is None
        assert llm_calls == []


class TestTier2LocalLlm:
    async def test_local_llm_replies_when_rules_miss(self):
        calls: list[str] = []

        async def local_llm(text: str) -> str | None:
            calls.append(text)
            return "这个我记下了。"

        router = make_router(local_llm=local_llm)
        result = await router.route("提醒我明天吃药")
        assert result.tier is RouterTier.LOCAL_LLM
        assert result.reply == "这个我记下了。"
        assert calls == ["提醒我明天吃药"]

    async def test_local_llm_none_falls_to_remote(self):
        async def local_llm(text: str) -> str | None:
            return None

        async def remote_llm(text: str) -> str | None:
            return "远程回答"

        router = make_router(local_llm=local_llm, remote_llm=remote_llm, remote_available=True)
        result = await router.route("随便一句话")
        assert result.tier is RouterTier.REMOTE_LLM
        assert result.reply == "远程回答"

    async def test_local_llm_unconfigured_goes_straight_to_remote(self):
        remote_calls: list[str] = []

        async def remote_llm(text: str) -> str | None:
            remote_calls.append(text)
            return "远程回答"

        router = make_router(remote_llm=remote_llm, remote_available=True)
        result = await router.route("随便一句话")
        assert result.tier is RouterTier.REMOTE_LLM
        assert remote_calls == ["随便一句话"]


class TestTier3RemoteAndDegrade:
    async def test_remote_unavailable_returns_unavailable(self):
        """降级：远程不可用时路由结果为 UNAVAILABLE（上层按 R27/R24 处理）。"""
        called: list[str] = []

        async def remote_llm(text: str) -> str | None:
            called.append(text)
            return "远程回答"

        router = make_router(remote_llm=remote_llm, remote_available=False)
        result = await router.route("复杂问题")
        assert result.tier is RouterTier.UNAVAILABLE
        assert result.reply is None
        assert called == [], "远程不可用时不得调用远程 handler"

    async def test_no_handlers_at_all_returns_unavailable(self):
        router = make_router()
        result = await router.route("复杂问题")
        assert result.tier is RouterTier.UNAVAILABLE

    async def test_remote_returns_none_degrades_to_unavailable(self):
        async def remote_llm(text: str) -> str | None:
            return None

        router = make_router(remote_llm=remote_llm, remote_available=True)
        result = await router.route("复杂问题")
        assert result.tier is RouterTier.UNAVAILABLE
