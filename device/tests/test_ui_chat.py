"""U3 聊天路由与页面测试（lira/ui/chat.py + templates/chat.html）。

策略（沿 test_ui.py / test_dev_console.py 的同步 TestClient 形态）：
  - 真 PrivacyState / PassphraseVault / ApplianceStore（:memory:）+ 真
    ChatStore（tmp_path）+ StubLlm 组装真实 ChatService，走完整路由面；
  - 同源守卫 / XSS 载荷 / 路由扫测（不改安全状态）/ chat=None 降级 / 成员
    生命周期事件（caplog）均测试化；
  - script 标签平衡回归（镜像 test_dev_console.py 模式）+ JS 提取
    node --check（node 不在 PATH 时跳过，Windows 侧按仓库纪律手动执行）。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lira.appliances.store import ApplianceStore
from lira.chat.service import ChatService
from lira.chat.store import ChatStore
from lira.dialog.phrasebook import (
    CHAT_EMPTY,
    CHAT_GREETING,
    CHAT_MEMBER_GONE,
    CHAT_PRIVACY,
    CHAT_TOO_LONG,
)
from lira.privacy import PassphraseVault, PrivacyState
from lira.ui.app import TEMPLATES_DIR, UiServices, create_app

PAGE_SIZE = 50

_UNSET = object()


# ---------- 测试基建 ----------


class StubLlm:
    """捕获调用参数的假 LLM（镜像 test_chat_service.StubLlm 的脚本语义）。"""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes) or ["好的呀"]
        self.calls: list[dict] = []

    async def chat(self, prompt, *, system=None, context=None,
                   on_wait_feedback=_UNSET, timeout_seconds=None, voice_log=True):
        self.calls.append({
            "prompt": prompt,
            "system": system,
            "context": context,
            "on_wait_feedback": on_wait_feedback,
            "timeout_seconds": timeout_seconds,
            "voice_log": voice_log,
        })
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def appliance_store() -> ApplianceStore:
    s = ApplianceStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def chat_store(tmp_path) -> ChatStore:
    s = ChatStore(tmp_path / "device.db")
    yield s
    s.close()


@pytest.fixture
def privacy() -> PrivacyState:
    return PrivacyState()


@pytest.fixture
def flags() -> dict:
    return {"remote": True}


@pytest.fixture
def llm() -> StubLlm:
    return StubLlm()


@pytest.fixture
def services(privacy, appliance_store, chat_store, flags, llm) -> UiServices:
    return UiServices(
        privacy=privacy,
        vault=PassphraseVault(appliance_store),
        store=appliance_store,
        chat=ChatService(
            chat_store,
            llm,
            privacy=lambda: privacy.is_on,
            remote_available=lambda: flags["remote"],
        ),
    )


@pytest.fixture
def app(services) -> FastAPI:
    return create_app(services)


@pytest.fixture
def client(app) -> TestClient:
    # with 形态：整个测试共用一个事件循环（ChatService 的 asyncio.Lock
    # 绑定首个使用它的 loop，跨请求必须同 loop）
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_no_chat(privacy, appliance_store) -> TestClient:
    app = create_app(UiServices(
        privacy=privacy,
        vault=PassphraseVault(appliance_store),
        store=appliance_store,
    ))
    with TestClient(app) as c:
        yield c


def make_member(services: UiServices, name: str = "妈妈") -> int:
    return services.chat.store.create_member(name)["id"]


def _assert_script_balance(html: str) -> None:
    """镜像 test_dev_console 的 script 平衡回归（丢标签 = 全页静默挂）。"""
    assert html.count("<script>") == html.count("</script>") >= 1
    assert re.search(r"<script>.*</script>", html, re.S), "script 块必须可完整提取"
    assert html.index("</script>") < html.index("</body>")


# ---------- AE2 / R5：选人页与成员创建 ----------


class TestPicker:
    def test_create_member_redirects_into_conversation_and_card_shows(self, client, services):
        """AE2：创建成员 → 303 进会话页；选人页出现成员卡片。"""
        resp = client.post(
            "/chat/members", data={"name": "妈妈", "emoji": "👵"}, follow_redirects=False
        )
        assert resp.status_code == 303
        mid = int(resp.headers["location"].rsplit("/", 1)[-1])
        member = services.chat.store.get_member(mid)
        assert member["name"] == "妈妈" and member["emoji"] == "👵"
        body = client.get("/chat").text
        assert f'href="/chat/{mid}"' in body
        assert "妈妈" in body and "👵" in body

    def test_zero_members_shows_first_member_form_only(self, client):
        """R5 首成员引导：零成员只呈现"创建第一位家人"表单。"""
        body = client.get("/chat").text
        assert "创建第一位家人" in body
        assert "添加家人" not in body
        assert 'action="/chat/members"' in body

    def test_default_emoji_applies_when_preset_missing(self, client, services):
        client.post("/chat/members", data={"name": "爷爷"}, follow_redirects=False)
        member = services.chat.store.list_members()[0]
        assert member["emoji"], "emoji 落库非空（预设缺省回落默认头像）"

    def test_create_duplicate_name_explicit_error(self, client):
        first = client.post("/chat/members", data={"name": "妈妈"}, follow_redirects=False)
        assert first.status_code == 303
        resp = client.post("/chat/members", data={"name": "妈妈"})
        assert resp.status_code == 400
        assert "这个名字已经有了" in resp.text

    def test_create_blank_name_400(self, client):
        resp = client.post("/chat/members", data={"name": "   "})
        assert resp.status_code == 400
        assert "称呼" in resp.text

    def test_non_preset_emoji_falls_back_to_default(self, client, services):
        """emoji 只收预设单选值；伪造值回落默认（不经任何属性拼接渲染）。"""
        client.post("/chat/members", data={"name": "舅舅", "emoji": "<script>"})
        member = services.chat.store.list_members()[0]
        assert member["emoji"] != "<script>"


# ---------- 会话页 ----------


class TestConversation:
    def test_unknown_member_404(self, client):
        assert client.get("/chat/999999").status_code == 404

    def test_zero_messages_renders_greeting_without_load_more(self, client):
        mid = make_member_id(client)
        body = client.get(f"/chat/{mid}").text
        assert CHAT_GREETING in body
        assert re.search(r'id="btn-more"[^>]*hidden', body), "零消息不渲染可用加载更多"

    def test_messages_rendered_oldest_to_newest(self, client, services):
        mid = make_member(services)
        services.chat.store.append_messages(
            mid, [("user", "第一句"), ("assistant", "第一答"), ("user", "第二句")]
        )
        body = client.get(f"/chat/{mid}").text
        assert body.index("第一句") < body.index("第一答") < body.index("第二句")

    def test_header_carries_member_emoji_and_name(self, client, services):
        mid = services.chat.store.create_member("奶奶", "👵")["id"]
        body = client.get(f"/chat/{mid}").text
        assert "奶奶" in body and "👵" in body

    def test_rename_success_keeps_history(self, client, services):
        mid = make_member(services)
        services.chat.store.append_messages(mid, [("user", "改名前的消息")])
        resp = client.post(f"/chat/{mid}/rename", data={"name": "母亲"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/chat/{mid}"
        body = client.get(f"/chat/{mid}").text
        assert "母亲" in body
        assert "改名前的消息" in body, "改名不断历史（按 id 外键）"

    def test_rename_duplicate_explicit_error(self, client, services):
        make_member(services, "爸爸")
        mid = make_member(services, "妈妈")
        resp = client.post(f"/chat/{mid}/rename", data={"name": "爸爸"})
        assert resp.status_code == 400
        assert "这个名字已经有了" in resp.text

    def test_rename_unknown_member_404(self, client):
        assert client.post("/chat/999999/rename", data={"name": "谁"}).status_code == 404

    def test_delete_redirects_to_picker_and_cascades(self, client, services):
        """删除 → 303 回选人页（与创建对称）；会话页 404、消息级联删除。"""
        mid = make_member(services)
        services.chat.store.append_messages(mid, [("user", "将被级联删除")])
        resp = client.post(f"/chat/{mid}/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/chat"
        assert client.get(f"/chat/{mid}").status_code == 404
        assert services.chat.store.recent_messages(mid) == [], "级联删除其全部消息"

    def test_delete_follow_redirect_lands_on_picker(self, client, services):
        """页内确认成功后跟随重定向到达选人页。"""
        mid = make_member(services)
        final = client.post(f"/chat/{mid}/delete")  # 默认跟随重定向
        assert final.status_code == 200
        assert "创建第一位家人" in final.text

    def test_delete_unknown_member_404(self, client):
        assert client.post("/chat/999999/delete", follow_redirects=False).status_code == 404

    def test_delete_confirm_markup_in_page(self, client):
        """页内确认（无原生 confirm）：确认块默认隐藏、未确认不提交。"""
        mid = make_member_id(client)
        body = client.get(f"/chat/{mid}").text
        assert 'id="del-confirm" hidden' in body
        assert "确定删除" in body
        assert "delConfirm.hidden) { ev.preventDefault(); }" in body


def make_member_id(client: TestClient) -> int:
    """经路由创建成员并取回 id（顺带覆盖创建路径）。"""
    resp = client.post("/chat/members", data={"name": "妈妈"}, follow_redirects=False)
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[-1])


# ---------- 发消息（AE3 / AE5，JSON 进出） ----------


class TestSendMessage:
    def test_send_returns_reply_json_and_persists(self, client, services, llm):
        """AE3 第一轮：路由接线把文本透传给 ChatService，回复 JSON 返回。"""
        mid = make_member(services)
        resp = client.post(f"/chat/{mid}/messages", json={"text": "今天天气怎么样"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ok"
        assert data["reply"] == "好的呀"
        assert data["saved"] is True
        assert llm.calls[0]["prompt"] == "今天天气怎么样"
        history = [r["content"] for r in services.chat.store.recent_messages(mid)]
        assert history == ["今天天气怎么样", "好的呀"]

    def test_second_send_carries_first_turn_context(self, client, services, llm):
        """AE3 连续两轮：第二轮路由调用携带第一轮上下文。"""
        mid = make_member(services)
        client.post(f"/chat/{mid}/messages", json={"text": "第一句"})
        resp = client.post(f"/chat/{mid}/messages", json={"text": "第二句"})
        assert resp.status_code == 200 and resp.json()["state"] == "ok"
        pairs = llm.calls[1]["context"].pairs
        assert [(p.user, p.assistant) for p in pairs] == [("第一句", "好的呀")]

    def test_privacy_on_degrades_structured_not_exception(self, client, services, llm, privacy):
        """AE5：真 PrivacyState 置 ON → 结构化 privacy 降级，而非异常/静默。"""
        mid = make_member(services)
        privacy._on = True
        resp = client.post(f"/chat/{mid}/messages", json={"text": "悄悄话"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "privacy"
        assert data["message"] == CHAT_PRIVACY
        assert data["reply"] is None
        assert data["saved"] is True, "隐私轮 user 消息仍落库（R7）"
        assert llm.calls == [], "隐私期零 LLM 调用"
        history = [r["content"] for r in services.chat.store.recent_messages(mid)]
        assert history == ["悄悄话"]

    def test_unknown_member_send_is_member_gone_json(self, client):
        resp = client.post("/chat/999999/messages", json={"text": "发谁呢"})
        assert resp.status_code == 200  # 数据端点：结构化降级而非 404/302
        data = resp.json()
        assert data["state"] == "member_gone"
        assert data["message"] == CHAT_MEMBER_GONE

    def test_empty_text_state_empty_no_llm(self, client, llm):
        mid = make_member_id(client)
        resp = client.post(f"/chat/{mid}/messages", json={"text": "   "})
        data = resp.json()
        assert data["state"] == "empty" and data["message"] == CHAT_EMPTY
        assert llm.calls == []

    def test_over_long_text_state_too_long(self, client, llm):
        mid = make_member_id(client)
        resp = client.post(f"/chat/{mid}/messages", json={"text": "字" * 2001})
        data = resp.json()
        assert data["state"] == "too_long" and data["message"] == CHAT_TOO_LONG
        assert llm.calls == []

    def test_non_json_content_type_rejected_415(self, client):
        """纵深防御：消息端点 Content-Type 限定 application/json（回 JSON 不 302）。"""
        mid = make_member_id(client)
        resp = client.post(f"/chat/{mid}/messages", data={"text": "表单来的"})
        assert resp.status_code == 415
        assert resp.headers["content-type"].startswith("application/json")

    def test_text_not_read_from_query_params(self, client, llm):
        """文本参数只走 POST body：query 里的 text 一律忽略。"""
        mid = make_member_id(client)
        resp = client.post(f"/chat/{mid}/messages?text=来自query", json={})
        assert resp.json()["state"] == "empty"
        assert all(c["prompt"] != "来自query" for c in llm.calls)

    def test_malformed_json_body_400(self, client):
        mid = make_member_id(client)
        resp = client.post(
            f"/chat/{mid}/messages",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.headers["content-type"].startswith("application/json")


# ---------- 历史分页（AE4） ----------


class TestPagination:
    def test_first_page_50_and_before_returns_older_only(self, client, services):
        """AE4：50+ 条时首屏 50 条，before 翻页只取更早。"""
        mid = make_member(services)
        services.chat.store.append_messages(
            mid, [("user", f"旧消息{i:02d}") for i in range(55)]
        )
        body = client.get(f"/chat/{mid}").text
        assert "旧消息54" in body and "旧消息05" in body, "首屏为最近 50 条"
        assert "旧消息04" not in body and "旧消息00" not in body
        assert not re.search(r'id="btn-more"[^>]*hidden', body), "还有更早历史时加载更多可见"

        data = client.get(f"/chat/{mid}/messages", params={"before": 6}).json()
        assert data["has_more"] is False
        assert [m["content"] for m in data["messages"]] == [f"旧消息{i:02d}" for i in range(5)]

    def test_middle_pages_are_50_and_strictly_older(self, client, services):
        mid = make_member(services)
        services.chat.store.append_messages(
            mid, [("user", f"m{i:03d}") for i in range(120)]
        )
        data = client.get(f"/chat/{mid}/messages", params={"before": 71}).json()
        assert data["has_more"] is True
        ids = [m["id"] for m in data["messages"]]
        assert len(ids) == PAGE_SIZE
        assert ids == list(range(21, 71)), "贴近 before 的一页且不含更新消息"
        data = client.get(f"/chat/{mid}/messages", params={"before": 21}).json()
        assert data["has_more"] is False
        assert [m["id"] for m in data["messages"]] == list(range(1, 21))

    def test_history_scoped_to_selected_member(self, client, services):
        """AE4：历史仅按所选成员呈现。"""
        mom = make_member(services, "妈妈")
        dad = make_member(services, "爸爸")
        services.chat.store.append_messages(mom, [("user", "妈妈的话")])
        services.chat.store.append_messages(dad, [("user", "爸爸的话")])
        mom_body = client.get(f"/chat/{mom}").text
        assert "妈妈的话" in mom_body and "爸爸的话" not in mom_body
        dad_page = client.get(f"/chat/{dad}/messages").json()
        assert [m["content"] for m in dad_page["messages"]] == ["爸爸的话"]

    def test_pagination_unknown_member_404_json(self, client):
        resp = client.get("/chat/999999/messages")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")

    def test_before_must_be_numeric(self, client):
        """全聊天面唯一 GET 查询参数 before 必须为数值。"""
        mid = make_member_id(client)
        resp = client.get(f"/chat/{mid}/messages", params={"before": "abc"})
        assert resp.status_code == 422

    def test_pagination_json_shape(self, client, services):
        mid = make_member(services)
        services.chat.store.append_messages(mid, [("user", "只有一条")])
        data = client.get(f"/chat/{mid}/messages").json()
        assert set(data) == {"messages", "has_more"}
        assert set(data["messages"][0]) == {"id", "role", "content"}


# ---------- 同源守卫 ----------


class TestOriginGuard:
    FOREIGN = {"Origin": "https://evil.example"}

    def test_foreign_origin_create_rejected(self, client):
        resp = client.post(
            "/chat/members", data={"name": "妈妈"}, headers=self.FOREIGN, follow_redirects=False
        )
        assert resp.status_code == 403
        assert "不被允许" in resp.text

    def test_foreign_origin_rename_rejected(self, client, services):
        mid = make_member(services)
        resp = client.post(
            f"/chat/{mid}/rename", data={"name": "改"}, headers=self.FOREIGN, follow_redirects=False
        )
        assert resp.status_code == 403
        assert services.chat.store.get_member(mid)["name"] == "妈妈", "未执行任何变更"

    def test_foreign_origin_delete_rejected(self, client, services):
        mid = make_member(services)
        resp = client.post(f"/chat/{mid}/delete", headers=self.FOREIGN, follow_redirects=False)
        assert resp.status_code == 403
        assert services.chat.store.get_member(mid) is not None

    def test_foreign_origin_send_rejected_json(self, client, services, llm):
        mid = make_member(services)
        resp = client.post(
            f"/chat/{mid}/messages", json={"text": "跨源"}, headers=self.FOREIGN
        )
        assert resp.status_code == 403
        assert resp.headers["content-type"].startswith("application/json")
        assert llm.calls == []

    def test_same_origin_create_allowed(self, client):
        """浏览器语义的同源 POST（Origin host == 请求 host）放行。"""
        resp = client.post(
            "/chat/members",
            data={"name": "妈妈"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_missing_origin_allowed(self, client):
        """缺失 Origin 放行（非浏览器 LAN actor 为既有局域网边界接受主体）。"""
        resp = client.post("/chat/members", data={"name": "妈妈"}, follow_redirects=False)
        assert resp.status_code == 303


# ---------- 路由扫测（R9：聊天面不改安全状态） ----------


class TestRouteScanSafety:
    def _chat_paths(self, app: FastAPI) -> list[str]:
        paths = {
            route.path for route in app.routes
            if getattr(route, "path", "").startswith("/chat")
        }
        assert len(paths) >= 6, "聊天路由应已全部注册"
        return sorted(paths)

    def test_scan_no_request_changes_security_state(self, app, client, services, privacy):
        """遍历全部 /chat* 路由：任何 GET/POST 均不改隐私状态与口令状态。"""
        make_member(services)
        paths = [p.replace("{member_id}", "999999") for p in self._chat_paths(app)]
        for path in paths:
            r1 = client.get(path, follow_redirects=False)
            assert r1.status_code < 500, f"GET {path} -> {r1.status_code}"
            r2 = client.post(path, json={"text": "x", "name": "x"}, follow_redirects=False)
            assert r2.status_code < 500, f"POST {path} -> {r2.status_code}"
            assert privacy.is_on is False, f"{path} 改变了隐私状态"
            assert not services.vault.is_set(), f"{path} 改变了口令状态"

        privacy._on = True  # 隐私 ON 侧再扫一遍：聊天请求也不得关闭隐私
        for path in paths:
            client.post(path, json={"text": "x"}, follow_redirects=False)
            assert privacy.is_on is True, f"隐私 ON 期间 {path} 改变了隐私状态"

    def test_chat_surface_needs_no_passphrase(self, client):
        """R9：聊天入口无口令——未设口令时选人/会话/发送全部可用。"""
        assert client.get("/chat").status_code == 200
        resp = client.post("/chat/members", data={"name": "妈妈"}, follow_redirects=False)
        assert resp.status_code == 303, "口令未设置不拦截成员创建"


# ---------- XSS 载荷回归（转义边界测试化） ----------


class TestXssEscaping:
    def test_message_payload_inert_in_server_render(self, client, services):
        mid = make_member(services)
        services.chat.store.append_messages(
            mid,
            [("user", "<script>alert(1)</script>"), ("assistant", "<img src=x onerror=alert(2)>")],
        )
        body = client.get(f"/chat/{mid}").text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "<script>alert" not in body
        assert "&lt;img src=x onerror" in body
        assert "<img src=x onerror" not in body

    def test_member_name_payload_inert_in_picker_and_conversation(self, client):
        """经路由创建载荷名：选人页与会话页均呈现惰性文本。"""
        resp = client.post("/chat/members", data={"name": "<script>"}, follow_redirects=False)
        assert resp.status_code == 303
        mid = int(resp.headers["location"].rsplit("/", 1)[-1])
        picker = client.get("/chat").text
        assert "&lt;script&gt;" in picker, "载荷名必须以转义形态出现"
        assert "&lt;script&gt;</span>" in picker, "成员卡片内为惰性文本"
        page = client.get(f"/chat/{mid}").text
        assert "&lt;script&gt;" in page
        assert "<script>alert" not in page

    def test_llm_reply_payload_inert_on_next_render(self, client, llm):
        """LLM 回复（role 不可信）同样只经转义边界。"""
        llm.outcomes = ["<img src=x onerror=alert(3)>"]
        mid = make_member_id(client)
        data = client.post(f"/chat/{mid}/messages", json={"text": "诱导"}).json()
        assert data["state"] == "ok" and "<img" in data["reply"]  # JSON 字符串原样携带
        body = client.get(f"/chat/{mid}").text
        assert "&lt;img src=x onerror" in body
        assert "<img src=x onerror" not in body

    def test_chat_template_has_no_safe_filter(self):
        """聊天模板禁用 safe 过滤器（机械检查）。"""
        source = (TEMPLATES_DIR / "chat.html").read_text(encoding="utf-8")
        assert "|safe" not in source
        assert "Markup(" not in source

    def test_js_insert_path_uses_textcontent_never_innerhtml(self, client):
        """JS 插入路径：动态文本只经 textContent 进 DOM（元素内容位置）。"""
        mid = make_member_id(client)
        page = client.get(f"/chat/{mid}").text
        assert "innerHTML" not in page, "内嵌 JS 不得使用 innerHTML 注入动态值"
        assert ".textContent =" in page
        assert "dataset" in page, "数值 ID 走 dataset/元素属性"

    def test_rendered_pages_script_tags_balanced(self, client, client_no_chat):
        """script 平衡回归（会话页）；选人页/暂不可用页不再携带会话页脚本。"""
        for page in (client.get("/chat").text, client_no_chat.get("/chat/1").text):
            assert page.count("<script>") == 0, "非会话页不携带页脚本"
        mid = make_member_id(client)
        _assert_script_balance(client.get(f"/chat/{mid}").text)

    def test_extracted_js_passes_node_check(self, client, tmp_path):
        """提取内嵌 JS 跑 node --check（node 不在 PATH 时跳过，Windows 手动执行）。"""
        mid = make_member_id(client)
        html = client.get(f"/chat/{mid}").text
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        assert scripts
        node = shutil.which("node")
        if node is None:
            pytest.skip("node 不在 PATH（按仓库纪律在 Windows 侧手动执行 node --check）")
        for i, js in enumerate(scripts):
            path = tmp_path / f"chat_js_{i}.js"
            path.write_text(js, encoding="utf-8")
            proc = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True, timeout=60
            )
            assert proc.returncode == 0, f"node --check 失败：{proc.stderr}"


# ---------- 成员生命周期事件日志（R10） ----------


class TestLifecycleEvents:
    def test_create_rename_delete_log_numeric_ids_only(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="lira.ui.chat"):
            resp = client.post("/chat/members", data={"name": "妈妈"}, follow_redirects=False)
            mid = int(resp.headers["location"].rsplit("/", 1)[-1])
            client.post(f"/chat/{mid}/rename", data={"name": "母亲"}, follow_redirects=False)
            client.post(f"/chat/{mid}/delete", follow_redirects=False)

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            f"event=member_created source=web-chat member_id={mid}" in m for m in messages
        )
        assert any(
            f"event=member_renamed source=web-chat member_id={mid}" in m for m in messages
        )
        assert any(
            f"event=member_deleted source=web-chat member_id={mid}" in m for m in messages
        )
        assert all("妈妈" not in m and "母亲" not in m for m in messages), "事件只记 ID 不记名字"

    def test_duplicate_create_rejection_logged_without_name(self, client, caplog):
        client.post("/chat/members", data={"name": "妈妈"}, follow_redirects=False)
        with caplog.at_level(logging.INFO, logger="lira.ui.chat"):
            client.post("/chat/members", data={"name": "妈妈"})
        messages = [r.getMessage() for r in caplog.records]
        assert any("event=member_create_rejected reason=duplicate" in m for m in messages)
        assert all("妈妈" not in m for m in messages)


# ---------- chat=None 装配（显式暂不可用，不 500、不静默） ----------


class TestChatUnavailableAssembly:
    def test_picker_renders_unavailable_note(self, client_no_chat):
        resp = client_no_chat.get("/chat")
        assert resp.status_code == 200
        assert "暂不可用" in resp.text

    def test_conversation_renders_unavailable_note(self, client_no_chat):
        resp = client_no_chat.get("/chat/1")
        assert resp.status_code == 200
        assert "暂不可用" in resp.text

    def test_member_endpoints_respond_explicitly(self, client_no_chat):
        for path, kwargs in (
            ("/chat/members", {"data": {"name": "妈妈"}}),
            ("/chat/1/rename", {"data": {"name": "改"}}),
            ("/chat/1/delete", {}),
        ):
            resp = client_no_chat.post(path, **kwargs, follow_redirects=False)
            assert resp.status_code == 503, f"{path} -> {resp.status_code}"
            assert "暂不可用" in resp.text, f"{path} 未显式说明"

    def test_message_endpoints_respond_json_503(self, client_no_chat):
        sent = client_no_chat.post("/chat/1/messages", json={"text": "你好"})
        assert sent.status_code == 503
        assert sent.json()["state"] == "unavailable"
        assert "暂不可用" in sent.json()["message"]
        listed = client_no_chat.get("/chat/1/messages")
        assert listed.status_code == 503
        assert listed.headers["content-type"].startswith("application/json")
        assert "暂不可用" in listed.json()["error"]

    def test_existing_pages_unaffected_by_chat_absence(self, client_no_chat):
        assert client_no_chat.get("/").status_code == 200
        assert client_no_chat.get("/status").status_code == 200


# ---------- 会话页结构（前端交互契约的服务端侧） ----------


class TestConversationStructure:
    def test_root_carries_numeric_datasets(self, client):
        mid = make_member_id(client)
        body = client.get(f"/chat/{mid}").text
        assert f'data-member-id="{mid}"' in body
        assert 'data-oldest-id' in body
        assert 'data-page-size' not in body, "分页大小由服务端 has_more 编码，不再下发"

    def test_send_form_and_more_button_present(self, client, services):
        mid = make_member(services)
        body = client.get(f"/chat/{mid}").text
        assert 'id="chat-form"' in body and 'id="btn-send"' in body
        assert 'id="btn-more"' in body
        assert "加载更多" in body
        assert "思考中" in body, "单飞 pending 态文案就位"

    def test_degrade_states_carry_retry_logic(self, client):
        """降级保气泡 + 一键重发 + 仅成功清空输入（JS 契约就位）。"""
        mid = make_member_id(client)
        page = client.get(f"/chat/{mid}").text
        assert "未发送" in page
        assert "重发" in page
        assert "j.state === 'ok'" in page
        assert "input.value = ''" in page
