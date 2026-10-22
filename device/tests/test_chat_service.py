"""U2 聊天服务测试（ChatService：滑窗组装、五态降级、护栏与内容纪律）。

策略：
  - stub LLM 捕获调用参数，断言聊天调用约束（on_wait_feedback=None、
    system=CHAT_SYSTEM_PROMPT、voice_log=False 预览抑制、独立超时）；
  - 真 ChatStore（tmp_path）验证落库语义（成对/单条/失败不落库/FK 竞态）；
  - caplog 断言任何路径日志只含事件/字数/耗时/ID，不落消息文本（R10）；
  - client.py 最小扩展回归：默认行为不变（语音路径），聊天传参生效。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace

from lira.chat.service import (
    CHAT_TIMEOUT_SECONDS,
    ChatService,
    MAX_MESSAGE_CHARS,
    WINDOW_MAX_CHARS,
    WINDOW_PAIRS,
)
from lira.chat.store import ChatStore
from lira.config import LlmConfig
from lira.dialog.context import ContextSnapshot
from lira.dialog.phrasebook import (
    CHAT_BUSY,
    CHAT_EMPTY,
    CHAT_FAILED,
    CHAT_GREETING,
    CHAT_MEMBER_GONE,
    CHAT_PRIVACY,
    CHAT_TOO_LONG,
    CHAT_UNAVAILABLE,
)
from lira.llm.client import LlmClient, LlmError, PrivacyBlocked
from lira.llm.prompts import CHAT_SYSTEM_PROMPT
from lira.voice_log import VOICE_LOG

# ---------- 测试基建 ----------

_UNSET = object()  # stub 缺省哨兵：断言服务端显式传了 on_wait_feedback=None


class StubLlm:
    """捕获调用参数的假 LLM：可脚本化回复/异常，可阻塞以测并发单飞。

    outcomes 每次调用弹出一项，耗尽后复用最后一项（沿 test_llm_client
    FakeRemote 的脚本语义）；`release` 置位前 chat() 挂起（模拟在途）；
    `on_call` 钩子在参数捕获后触发（模拟"发送中成员被删"竞态）。
    """

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.release: asyncio.Event | None = None
        self.on_call = None

    async def chat(self, prompt, *, system=None, context=None,
                   on_wait_feedback=_UNSET, timeout_seconds=None,
                   voice_log=True) -> str:
        self.calls.append({
            "prompt": prompt,
            "system": system,
            "context": context,
            "on_wait_feedback": on_wait_feedback,
            "timeout_seconds": timeout_seconds,
            "voice_log": voice_log,
        })
        if self.on_call is not None:
            self.on_call(self.calls[-1])
        if self.release is not None:
            await self.release.wait()
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    """可推进的假时钟（限流固定窗口测试用）。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def as_predicate(value):
    """bool 或 callable 统一包成谓词（镜像 LlmClient 的 privacy 注入形态）。"""
    return value if callable(value) else (lambda: value)


def make_service(store: ChatStore, llm: StubLlm, *, privacy=False,
                 remote_available=True, **kwargs) -> ChatService:
    return ChatService(
        store,
        llm,
        privacy=as_predicate(privacy),
        remote_available=as_predicate(remote_available),
        **kwargs,
    )


def make_member(store: ChatStore, name: str = "妈妈") -> int:
    return store.create_member(name)["id"]


class BrokenAppendStore(ChatStore):
    """只在追加时失败的库（模拟落库 IO 故障）。"""

    def append_messages(self, member_id, rows):
        raise sqlite3.OperationalError("simulated storage failure")


# ---------- 文案与提示词纪律（计划 Verification） ----------


class TestCopyAndPromptDiscipline:
    def test_privacy_copy_wording_precision(self):
        """隐私降级文案只断言"未发送至远程/云端"，禁止"未离开设备"式过度承诺。"""
        assert "远程" in CHAT_PRIVACY or "云端" in CHAT_PRIVACY
        assert "未离开设备" not in CHAT_PRIVACY
        assert "没离开" not in CHAT_PRIVACY

    def test_chat_prompt_same_persona_no_appliance_or_reading_promises(self):
        """人设与语音端同源（小丽拉），且明确不承诺控家电/拍照读字（R6/R9）。"""
        assert "小丽拉" in CHAT_SYSTEM_PROMPT
        assert "不能操作家电" in CHAT_SYSTEM_PROMPT
        assert "拍照" in CHAT_SYSTEM_PROMPT or "识别文字" in CHAT_SYSTEM_PROMPT
        assert "不要承诺" in CHAT_SYSTEM_PROMPT  # 也不承诺稍后代为执行

    def test_degrade_copies_present_and_content_free(self):
        """五态文案 + 超长/空消息/空会话引导语齐备（U3 依赖），均非空。"""
        for copy in (CHAT_PRIVACY, CHAT_UNAVAILABLE, CHAT_BUSY, CHAT_FAILED,
                     CHAT_MEMBER_GONE, CHAT_TOO_LONG, CHAT_EMPTY, CHAT_GREETING):
            assert copy


# ---------- Happy path ----------


class TestHappyPath:
    async def test_send_returns_reply_and_persists_pair_in_order(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("今天晴，适合出门")
        service = make_service(store, llm)

        result = await service.send(mid, "今天天气怎么样")

        assert result.state == "ok"
        assert result.reply == "今天晴，适合出门"
        assert result.saved is True
        history = [(r["role"], r["content"]) for r in store.recent_messages(mid)]
        assert history == [("user", "今天天气怎么样"), ("assistant", "今天晴，适合出门")]

    async def test_second_call_window_contains_first_turn(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("第一轮回复", "第二轮回复")
        service = make_service(store, llm)

        await service.send(mid, "第一轮问题")
        await service.send(mid, "第二轮问题")

        first, second = llm.calls
        assert first["prompt"] == "第一轮问题"
        assert first["context"].pairs == ()  # 首轮无历史
        assert second["prompt"] == "第二轮问题"
        assert [(p.user, p.assistant) for p in second["context"].pairs] == [
            ("第一轮问题", "第一轮回复")
        ]

    async def test_message_persisted_stripped(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        service = make_service(store, StubLlm("好的"))
        await service.send(mid, "  两边有空格  ")
        assert [r["content"] for r in store.recent_messages(mid)][0] == "两边有空格"


# ---------- 聊天调用约束（铁律，stub 捕获断言） ----------


class TestChatCallContract:
    async def test_llm_call_args_follow_chat_iron_rules(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("回复")
        service = make_service(store, llm)

        await service.send(mid, "问题")

        call = llm.calls[0]
        assert call["on_wait_feedback"] is None, "聊天永不触发设备发声（显式 None）"
        assert call["system"] is CHAT_SYSTEM_PROMPT
        assert call["voice_log"] is False, "回复预览/交互中条目不进 VOICE_LOG"
        assert call["timeout_seconds"] == CHAT_TIMEOUT_SECONDS
        context = call["context"]
        assert isinstance(context, ContextSnapshot)
        assert context.task is None, "阅读任务事实绝不进入聊天调用"
        assert all(m["role"] in ("user", "assistant") for m in context.messages())

    async def test_no_task_facts_in_any_captured_call(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("回复一", "回复二")
        service = make_service(store, llm)
        await service.send(mid, "问一")
        await service.send(mid, "问二")
        for call in llm.calls:
            assert call["context"].task is None
            assert "阅读任务" not in call["prompt"]


# ---------- 校验（不调 LLM、不落库） ----------


class TestValidation:
    async def test_empty_and_whitespace_rejected_without_llm(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("不应被调用")
        service = make_service(store, llm)

        for text in ("", "   ", "\n\t "):
            result = await service.send(mid, text)
            assert result.state == "empty"
            assert result.message == CHAT_EMPTY
            assert result.saved is False

        assert llm.calls == []
        assert store.recent_messages(mid) == []

    async def test_over_long_rejected_without_llm(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("不应被调用")
        service = make_service(store, llm)

        result = await service.send(mid, "字" * (MAX_MESSAGE_CHARS + 1))

        assert result.state == "too_long"
        assert result.message == CHAT_TOO_LONG
        assert llm.calls == []
        assert store.recent_messages(mid) == []

    async def test_exactly_max_chars_accepted(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        service = make_service(store, StubLlm("回复"))
        result = await service.send(mid, "字" * MAX_MESSAGE_CHARS)
        assert result.state == "ok"


# ---------- 降级态：privacy / unavailable / failed ----------


class TestPrivacyDegrade:
    async def test_privacy_on_persists_user_message_without_llm(self, tmp_path):
        """R7+R8：隐私 ON → privacy 文案、user 消息单独落库、零 LLM 调用。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("不应被调用")
        service = make_service(store, llm, privacy=True)

        result = await service.send(mid, "悄悄话")

        assert result.state == "privacy"
        assert result.message == CHAT_PRIVACY
        assert result.saved is True
        assert llm.calls == []
        history = [(r["role"], r["content"]) for r in store.recent_messages(mid)]
        assert history == [("user", "悄悄话")]  # 无 assistant 配对，计入 500 上限

    async def test_window_skips_unpaired_privacy_tail_on_next_turn(self, tmp_path):
        """隐私轮遗留的未配对尾部绝不进入下一轮窗口。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        flag = {"on": True}
        llm = StubLlm("正常回复")
        service = make_service(store, llm, privacy=lambda: flag["on"])

        await service.send(mid, "隐私期悄悄话")
        flag["on"] = False
        result = await service.send(mid, "正常问题")

        assert result.state == "ok"
        call = llm.calls[0]
        assert call["prompt"] == "正常问题"
        assert call["context"].pairs == (), "未配对尾部不作为上下文回传"
        history = [r["content"] for r in store.recent_messages(mid)]
        assert history == ["隐私期悄悄话", "正常问题", "正常回复"]

    async def test_privacy_blocked_mid_send_maps_to_privacy_state(self, tmp_path):
        """服务端检查与客户端入口之间的开关竞态：PrivacyBlocked 同样按隐私轮落库。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm(PrivacyBlocked("隐私模式开启，已拦截远程调用"))
        service = make_service(store, llm)

        result = await service.send(mid, "竞态消息")

        assert result.state == "privacy"
        assert result.saved is True
        history = [(r["role"], r["content"]) for r in store.recent_messages(mid)]
        assert history == [("user", "竞态消息")]


class TestUnavailableDegrade:
    async def test_remote_unavailable_no_llm_no_persist(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("不应被调用")
        service = make_service(store, llm, remote_available=False)

        result = await service.send(mid, "发不出去的消息")

        assert result.state == "unavailable"
        assert result.message == CHAT_UNAVAILABLE
        assert result.saved is False
        assert llm.calls == []
        assert store.recent_messages(mid) == []


class TestFailureDegrade:
    async def test_llm_error_persists_nothing(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm(LlmError("远程服务连接失败"))
        service = make_service(store, llm)

        result = await service.send(mid, "会失败的消息")

        assert result.state == "failed"
        assert result.message == CHAT_FAILED
        assert result.saved is False
        assert store.recent_messages(mid) == []

    async def test_retry_after_failure_is_clean_no_duplicates(self, tmp_path):
        """失败轮不落库：重试成功后历史完整、无失败轮残留。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm(LlmError("远程服务连接失败"), "恢复的回复")
        service = make_service(store, llm)

        assert (await service.send(mid, "第一次")).state == "failed"
        result = await service.send(mid, "第二次")

        assert result.state == "ok"
        history = [(r["role"], r["content"]) for r in store.recent_messages(mid)]
        assert history == [("user", "第二次"), ("assistant", "恢复的回复")]
        assert llm.calls[1]["context"].pairs == ()  # 失败轮未进上下文


# ---------- 落库失败与成员删除竞态 ----------


class TestStoreFailures:
    async def test_llm_ok_store_raises_returns_reply_with_unsaved_marker(self, tmp_path):
        """落库失败：回复仍返回 + saved=False 标记 + 事件日志（非静默、非 500）。"""
        store = BrokenAppendStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("宝贵的回复")
        service = make_service(store, llm)

        result = await service.send(mid, "这条可能存不上")

        assert result.state == "ok"
        assert result.reply == "宝贵的回复"
        assert result.saved is False
        assert store.recent_messages(mid) == []

    async def test_member_deleted_mid_send_maps_to_member_gone(self, tmp_path):
        """发送中成员被删：落库 FK 兜底 → member_gone（store 单事务已回滚）。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("迟到的回复")
        llm.on_call = lambda _call: store.delete_member(mid)
        service = make_service(store, llm)

        result = await service.send(mid, "发送期间被删")

        assert result.state == "member_gone"
        assert result.message == CHAT_MEMBER_GONE
        assert store.get_member(mid) is None
        assert store.recent_messages(mid) == []  # 单事务回滚，不留孤儿消息

    async def test_unknown_member_rejected_early_without_llm(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        llm = StubLlm("不应被调用")
        service = make_service(store, llm)
        result = await service.send(999, "不存在成员的消息")
        assert result.state == "member_gone"
        assert llm.calls == []


# ---------- 护栏：全局单飞 + 每成员限流 ----------


class TestGuards:
    async def test_second_send_while_in_flight_gets_busy(self, tmp_path):
        """在途调用期间（含等待 LLM），同成员二次发送得 busy。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("慢回复")
        llm.release = asyncio.Event()
        service = make_service(store, llm)

        first = asyncio.create_task(service.send(mid, "第一条"))
        while not llm.calls:
            await asyncio.sleep(0)  # 等 first 进入在途等待
        second = await service.send(mid, "第二条")

        assert second.state == "busy"
        assert second.message == CHAT_BUSY
        assert len(llm.calls) == 1, "在途期间不发起第二次 LLM 调用"

        llm.release.set()
        assert (await first).state == "ok"
        history = [(r["role"], r["content"]) for r in store.recent_messages(mid)]
        assert history == [("user", "第一条"), ("assistant", "慢回复")]

    async def test_cross_member_second_send_also_busy(self, tmp_path):
        """设备级全局单锁：成员 A 在途期间成员 B 也得 busy（非按成员隔离）。"""
        store = ChatStore(tmp_path / "device.db")
        a = make_member(store, "妈妈")
        b = make_member(store, "爸爸")
        llm = StubLlm("慢回复")
        llm.release = asyncio.Event()
        service = make_service(store, llm)

        first = asyncio.create_task(service.send(a, "妈妈的头一条"))
        while not llm.calls:
            await asyncio.sleep(0)
        second = await service.send(b, "爸爸的头一条")

        assert second.state == "busy"
        assert len(llm.calls) == 1
        llm.release.set()
        assert (await first).state == "ok"

    async def test_rate_limit_exceeded_busy_with_event(self, tmp_path, caplog):
        """每成员固定窗口限流：超限得 busy + rate_limited 事件，LLM 不被调用。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        llm = StubLlm("回复")
        service = make_service(store, llm, rate_limit=2)

        assert (await service.send(mid, "一")).state == "ok"
        assert (await service.send(mid, "二")).state == "ok"
        with caplog.at_level(logging.INFO, logger="lira.chat.service"):
            third = await service.send(mid, "三")

        assert third.state == "busy"
        assert third.message == CHAT_BUSY
        assert len(llm.calls) == 2
        assert any("event=rate_limited" in r.getMessage() for r in caplog.records)

    async def test_rate_limit_window_resets(self, tmp_path):
        """固定窗口翻窗/重启语义：窗口过后重新计数。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        clock = FakeClock()
        llm = StubLlm("回复")
        service = make_service(store, llm, now=clock, rate_limit=1, rate_window_seconds=60.0)

        assert (await service.send(mid, "第一条")).state == "ok"
        assert (await service.send(mid, "第二条")).state == "busy"
        clock.advance(61.0)
        assert (await service.send(mid, "第三条")).state == "ok"

    async def test_rate_limit_is_per_member(self, tmp_path):
        """限流按成员计数：A 超限不影响 B。"""
        store = ChatStore(tmp_path / "device.db")
        a = make_member(store, "妈妈")
        b = make_member(store, "爸爸")
        llm = StubLlm("回复")
        service = make_service(store, llm, rate_limit=1)
        assert (await service.send(a, "A 一")).state == "ok"
        assert (await service.send(a, "A 二")).state == "busy"
        assert (await service.send(b, "B 一")).state == "ok"


# ---------- 滑窗：对数上界 + 字符预算 ----------


class TestWindow:
    async def test_pair_cap_keeps_newest_eight(self, tmp_path):
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        for i in range(9):
            store.append_messages(mid, [("user", f"u{i}"), ("assistant", f"a{i}")])
        llm = StubLlm("回复")
        service = make_service(store, llm)

        await service.send(mid, "最新一条")

        pairs = llm.calls[0]["context"].pairs
        assert len(pairs) == WINDOW_PAIRS
        assert pairs[0].user == "u1"  # 最旧一对被对数上界裁掉
        assert pairs[-1].assistant == "a8"

    async def test_char_budget_trims_oldest(self, tmp_path):
        """预算内自最旧端裁剪（镜像 ConversationHistory 的 popleft 语义）。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        big = "字" * 1000  # 每对 2000 字符，3 对共 6000 > 4000
        for _ in range(3):
            store.append_messages(mid, [("user", big), ("assistant", big)])
        llm = StubLlm("回复")
        service = make_service(store, llm)

        await service.send(mid, "新消息")

        pairs = llm.calls[0]["context"].pairs
        assert len(pairs) == 2
        assert sum(len(p.user) + len(p.assistant) for p in pairs) <= WINDOW_MAX_CHARS

    async def test_newest_pair_over_budget_window_empty_but_call_proceeds(self, tmp_path):
        """最新一对单独超预算 → 窗口为空仍正常调用（不带过期上下文）。"""
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        store.append_messages(
            mid, [("user", "长" * 2500), ("assistant", "复" * 2500)]
        )
        llm = StubLlm("回复")
        service = make_service(store, llm)

        result = await service.send(mid, "不带上下文的新消息")

        assert result.state == "ok"
        assert llm.calls[0]["context"].pairs == ()
        assert llm.calls[0]["prompt"] == "不带上下文的新消息"


# ---------- 日志纪律（R10） ----------


class TestLogDiscipline:
    async def test_no_message_text_in_logs_on_any_path(self, tmp_path, caplog):
        secret = "玄武岩七号密语"
        store = ChatStore(tmp_path / "device.db")
        mid = make_member(store)
        clock = FakeClock()
        # 脚本：privacy(客户端竞态) → failed → ok
        llm = StubLlm(PrivacyBlocked("拦截"), LlmError("连接失败"), "正常回复")
        flag = {"on": True, "up": True}
        service = make_service(
            store,
            llm,
            privacy=lambda: flag["on"],
            remote_available=lambda: flag["up"],
            now=clock,
            rate_limit=100,
        )

        with caplog.at_level(logging.INFO):
            await service.send(mid, f"隐私轮{secret}")  # privacy（服务端检查）
            flag["on"] = False
            await service.send(mid, f"竞态轮{secret}")  # privacy（客户端拦截）
            await service.send(mid, f"失败轮{secret}")  # failed
            await service.send(mid, f"成功轮{secret}")  # ok
            flag["up"] = False
            await service.send(mid, f"断网轮{secret}")  # unavailable
            flag["up"] = True
            await service.send(mid, f"{secret}超长" + "字" * MAX_MESSAGE_CHARS)
            await service.send(mid, "   ")  # empty（纯空白拒绝）
            await service.send(999, f"未知成员{secret}")  # member_gone
            await service._mutex.acquire()
            try:
                await service.send(mid, f"占线轮{secret}")  # busy（全局单飞）
            finally:
                service._mutex.release()
            limited = make_service(store, StubLlm("回复"), rate_limit=1, now=clock)
            await limited.send(mid, "限流一")
            await limited.send(mid, f"限流轮{secret}")  # rate_limited

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in logged, "任何路径日志都不得出现消息文本"
        # 事件齐备：成功/失败/限流/占线/降级各路径均有结构化事件
        for event in ("event=ok", "event=fail", "event=rate_limited", "event=busy",
                      "event=privacy", "event=unavailable", "event=member_gone",
                      "event=reject", "event=send"):
            assert event in logged, f"缺少事件 {event}"
        assert "source=web-chat" in logged

    async def test_save_failed_event_logged_not_silent(self, tmp_path, caplog):
        store = BrokenAppendStore(tmp_path / "device.db")
        mid = make_member(store)
        service = make_service(store, StubLlm("回复"))
        with caplog.at_level(logging.INFO):
            await service.send(mid, "存不上的消息")
        assert any("event=save_failed" in r.getMessage() for r in caplog.records)


# ---------- client.py 最小扩展回归（语音路径默认行为不变） ----------


def make_llm_cfg(**overrides) -> LlmConfig:
    values = dict(
        base_url="http://unit.test/v1",
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


def make_stream_sdk(captured: dict):
    """单次返回一条增量的假 SDK；create 的 kwargs 全量捕获。"""

    class Stream:
        delivered = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.delivered:
                raise StopAsyncIteration
            self.delivered = True
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="回复文本"))]
            )

        async def close(self):
            pass

    async def create(**kwargs):
        captured.update(kwargs)
        return Stream()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class TestLlmClientChatOptions:
    def setup_method(self):
        VOICE_LOG.clear()

    async def test_default_call_still_writes_voice_log(self):
        """语音路径默认行为不变：交互中 + 回复预览照旧写入 VOICE_LOG。"""
        captured: dict = {}
        client = LlmClient(make_llm_cfg(), openai_client=make_stream_sdk(captured))
        assert await client.chat("问题") == "回复文本"
        kinds = [e.kind for e in VOICE_LOG.snapshot()]
        assert kinds == ["llm", "llm_reply"]

    async def test_voice_log_false_suppresses_every_entry(self):
        captured: dict = {}
        client = LlmClient(make_llm_cfg(), openai_client=make_stream_sdk(captured))
        assert await client.chat("问题", voice_log=False) == "回复文本"
        assert VOICE_LOG.snapshot() == [], "聊天调用不进语音展示面"

    async def test_timeout_override_reaches_create_only_when_given(self):
        captured: dict = {}
        client = LlmClient(make_llm_cfg(), openai_client=make_stream_sdk(captured))
        await client.chat("问题", timeout_seconds=CHAT_TIMEOUT_SECONDS)
        assert captured["timeout"] == CHAT_TIMEOUT_SECONDS

        captured.clear()
        await client.chat("第二问")
        assert "timeout" not in captured, "不传参时沿用构造时配置超时（语音不变）"
