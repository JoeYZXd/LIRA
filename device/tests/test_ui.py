"""U7 设备端触摸屏 UI 测试（R17/R12，fastapi TestClient + httpx）。

覆盖：
  - 状态卡/设备列表只读不鉴权；/status JSON
  - 口令首启设置流程（无默认值；不一致/过短 → 400；成功 → 入库哈希）
  - UI 隐私开关需口令：未设置口令 → 303 引导设置页（fail-closed）；
    错口令 → 401 且状态不变；对口令 → 殊途同归触碰同一 PrivacyState
  - 音量/语速设置（越界/非法 → 400）
  - 首页改版（home-console U5）：时段问候三分支 / 聊天入口（chat=None 显式
    暂不可用）/ 音量语速步进表单 / 运维信息收纳 `<details>` 折叠区（AE1）
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from lira.appliances.models import ApplianceModel
from lira.appliances.store import ApplianceStore
from lira.privacy import PassphraseVault, PrivacyState
from lira.ui.app import (
    HOME_WELCOME,
    DeviceSettings,
    UiServices,
    create_app,
    greeting_for_hour,
    home_greeting,
)


@pytest.fixture
def store(tmp_path) -> ApplianceStore:
    s = ApplianceStore(tmp_path / "device.db")
    yield s
    s.close()


@pytest.fixture
def privacy() -> PrivacyState:
    return PrivacyState()


@pytest.fixture
def flags() -> dict[str, bool]:
    return {"remote": False, "network": False}


@pytest.fixture
def services(privacy: PrivacyState, store: ApplianceStore, flags) -> UiServices:
    return UiServices(
        privacy=privacy,
        vault=PassphraseVault(store),
        store=store,
        settings=DeviceSettings(),
        remote_available=lambda: flags["remote"],
        network_ok=lambda: flags["network"],
    )


@pytest.fixture
def client(services: UiServices) -> TestClient:
    return TestClient(create_app(services))


def setup_passphrase(client: TestClient, passphrase: str = "1234") -> None:
    resp = client.post(
        "/passphrase/setup",
        data={"passphrase": passphrase, "confirm": passphrase},
        follow_redirects=False,
    )
    assert resp.status_code == 303


# ---------- U5 首页改版：测试基建 ----------


class _ChatStub:
    """首页只判空 services.chat；占位对象即代表"聊天已装配"。"""


class _DevStub:
    """mount_dev 仅经 handle.store 取签名密钥；鸭子类型占位即可挂载。"""

    def __init__(self, store: ApplianceStore) -> None:
        self.store = store


def _details_block(body: str) -> str:
    """`<details>` 折叠区片段（AE1：运维信息应收纳于此）。"""
    start = body.index("<details")
    end = body.index("</details>") + len("</details>")
    return body[start:end]


@pytest.fixture
def client_with_chat(services: UiServices) -> TestClient:
    """聊天已装配形态的首页客户端（默认 services 的 chat=None）。"""
    services.chat = _ChatStub()
    return TestClient(create_app(services))


# ---------- 只读状态（不鉴权） ----------


class TestReadOnlyStatus:
    def test_index_renders_status_card_and_devices(self, client, store):
        store.upsert_appliance(
            ApplianceModel(
                name="取暖器",
                aliases=("取暖器", "小暖"),
                actions={"打开": ("打开",), "关闭": ("关闭",)},
                codes={"打开": "raw:1 2 3"},
                is_high_risk=True,
                enabled=False,
            )
        )
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.text
        assert "隐私模式" in body
        assert "取暖器" in body and "小暖" in body
        assert "高危" in body and "已禁用" in body
        assert "raw:1 2 3" not in body, "码值是屏上不该出现的内容（只读视图不外露）"

    def test_status_json_no_auth(self, client, services):
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["privacy_on"] is False
        assert data["passphrase_set"] is False
        assert data["volume"] == 1.0

    def test_status_reflects_flags(self, client, flags):
        flags["network"] = True
        flags["remote"] = True
        data = client.get("/status").json()
        assert data["network_ok"] is True
        assert data["remote_available"] is True


# ---------- 口令首启设置（无默认值） ----------


class TestPassphraseSetup:
    def test_index_guides_to_setup_when_not_set(self, client):
        body = client.get("/").text
        assert "尚未设置设备口令" in body
        assert "/passphrase/setup" in body

    def test_setup_page_available(self, client):
        resp = client.get("/passphrase/setup")
        assert resp.status_code == 200
        assert "设置设备口令" in resp.text

    def test_setup_mismatch_rejected(self, client):
        resp = client.post(
            "/passphrase/setup",
            data={"passphrase": "1234", "confirm": "5678"},
        )
        assert resp.status_code == 400
        assert "两次输入不一致" in resp.text

    def test_setup_too_short_rejected(self, client):
        resp = client.post(
            "/passphrase/setup",
            data={"passphrase": "abc", "confirm": "abc"},
        )
        assert resp.status_code == 400

    def test_setup_success_redirects_and_persists(self, client, services):
        setup_passphrase(client)
        assert services.vault.is_set() is True
        body = client.get("/").text
        assert "尚未设置设备口令" not in body

    def test_privacy_without_passphrase_redirects_to_setup_fail_closed(
        self, client, services
    ):
        resp = client.post(
            "/privacy",
            data={"action": "on", "passphrase": "whatever"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/passphrase/setup"
        assert services.privacy.is_on is False, "口令未设置绝不改安全状态"


# ---------- 口令覆盖需验旧口令（SEC-2） ----------


class TestPassphraseOverwrite:
    def test_setup_page_asks_old_passphrase_when_already_set(self, client, tmp_path):
        setup_passphrase(client, "old-pass")
        body = client.get("/passphrase/setup").text
        assert 'name="old_passphrase"' in body
        # 首启页面（口令未设置）不得出现旧口令输入（无凭据可验）
        fresh_store = ApplianceStore(tmp_path / "fresh.db")
        fresh = TestClient(
            create_app(UiServices(
                privacy=PrivacyState(),
                vault=PassphraseVault(fresh_store),
                store=fresh_store,
            ))
        )
        assert "old_passphrase" not in fresh.get("/passphrase/setup").text

    def test_overwrite_without_old_passphrase_rejected(self, client, services):
        """SEC-2 核心用例：已设口令后不带旧口令直接覆盖 → 403，旧口令保持有效。"""
        setup_passphrase(client, "old-pass")
        resp = client.post(
            "/passphrase/setup",
            data={"passphrase": "new-pass", "confirm": "new-pass"},
        )
        assert resp.status_code == 403
        assert "当前口令不正确" in resp.text
        assert services.vault.verify("old-pass"), "旧口令必须保持有效"
        assert not services.vault.verify("new-pass"), "无凭据覆盖绝不生效"

    def test_overwrite_with_wrong_old_passphrase_rejected(self, client, services):
        setup_passphrase(client, "old-pass")
        resp = client.post(
            "/passphrase/setup",
            data={"old_passphrase": "wrong-pass", "passphrase": "new-pass",
                  "confirm": "new-pass"},
        )
        assert resp.status_code == 403
        assert services.vault.verify("old-pass")
        assert not services.vault.verify("new-pass")

    def test_overwrite_with_correct_old_passphrase_succeeds(self, client, services):
        setup_passphrase(client, "old-pass")
        resp = client.post(
            "/passphrase/setup",
            data={"old_passphrase": "old-pass", "passphrase": "new-pass",
                  "confirm": "new-pass"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert services.vault.verify("new-pass"), "出示正确旧口令后覆盖成功"
        assert not services.vault.verify("old-pass"), "旧口令随覆盖失效"


# ---------- UI 隐私开关（需口令；殊途同归） ----------


class TestPrivacyToggle:
    def test_wrong_passphrase_401_state_unchanged(self, client, services):
        setup_passphrase(client, "right-pass")
        resp = client.post(
            "/privacy",
            data={"action": "on", "passphrase": "wrong-pass"},
        )
        assert resp.status_code == 401
        assert "口令不正确" in resp.text
        assert services.privacy.is_on is False

    def test_correct_passphrase_toggles_on(self, client, services):
        setup_passphrase(client, "1234")
        resp = client.post(
            "/privacy",
            data={"action": "on", "passphrase": "1234"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert services.privacy.is_on is True
        body = client.get("/").text
        assert "隐私模式：开启" in body
        assert client.get("/status").json()["privacy_on"] is True

    def test_invalid_action_400(self, client):
        setup_passphrase(client)
        resp = client.post("/privacy", data={"action": "maybe", "passphrase": "1234"})
        assert resp.status_code == 400


# ---------- 音量 / 语速 ----------


class TestSettingsEndpoints:
    def test_volume_update(self, client, services):
        setup_passphrase(client)  # 与安全无关，仅走同一入口页
        resp = client.post("/volume", data={"value": "1.5"}, follow_redirects=False)
        assert resp.status_code == 303
        assert services.settings.volume == 1.5

    def test_speed_update(self, client, services):
        resp = client.post("/speed", data={"value": "0.8"}, follow_redirects=False)
        assert resp.status_code == 303
        assert services.settings.tts_speed == 0.8

    def test_volume_out_of_range_400_unchanged(self, client, services):
        resp = client.post("/volume", data={"value": "9.9"})
        assert resp.status_code == 400
        assert services.settings.volume == 1.0

    def test_volume_non_numeric_400(self, client):
        resp = client.post("/volume", data={"value": "loud"})
        assert resp.status_code == 400


# ---------- 首页改版：时段问候（U5） ----------


class TestHomeGreeting:
    def test_morning_branch(self):
        assert greeting_for_hour(5) == "早上好"
        assert greeting_for_hour(8) == "早上好"
        assert greeting_for_hour(11) == "早上好"

    def test_afternoon_branch(self):
        assert greeting_for_hour(12) == "下午好"
        assert greeting_for_hour(14) == "下午好"
        assert greeting_for_hour(17) == "下午好"

    def test_evening_branch(self):
        assert greeting_for_hour(18) == "晚上好"
        assert greeting_for_hour(21) == "晚上好"
        assert greeting_for_hour(23) == "晚上好"

    def test_late_night_falls_into_evening(self):
        """深夜凌晨归晚上：三分支覆盖全部 24 小时，无第四态。"""
        assert greeting_for_hour(0) == "晚上好"
        assert greeting_for_hour(4) == "晚上好"

    def test_full_greeting_pairs_slot_with_welcome(self):
        """问候整句 = 时段问候 + 固定家庭助手欢迎语。"""
        assert home_greeting(8) == f"早上好，{HOME_WELCOME}"
        assert home_greeting(14).startswith("下午好")
        assert home_greeting(2).startswith("晚上好")
        assert all(HOME_WELCOME in home_greeting(h) for h in range(24))

    def test_index_hero_carries_greeting_above_chat_entry(self, client_with_chat):
        body = client_with_chat.get("/").text
        assert HOME_WELCOME in body
        assert body.index(HOME_WELCOME) < body.index('href="/chat"'), "问候先于聊天入口"


# ---------- 首页改版：布局收纳（AE1，U5） ----------


class TestHomeLayout:
    def test_first_screen_greeting_and_chat_entry(self, client_with_chat):
        body = client_with_chat.get("/").text
        assert HOME_WELCOME in body
        assert "和小丽拉聊天" in body
        assert 'href="/chat"' in body

    def test_page_order_hero_quick_privacy_then_details(self, client_with_chat):
        """首屏 → 快捷区 → 隐私卡（显著但不压首屏）→ 折叠区。"""
        setup_passphrase(client_with_chat)  # 口令已设才渲染隐私开关表单
        body = client_with_chat.get("/").text
        assert body.index(HOME_WELCOME) < body.index('href="/chat"')
        assert body.index('href="/chat"') < body.index('action="/volume"')
        assert body.index('action="/volume"') < body.index('action="/privacy"')
        assert body.index('action="/privacy"') < body.index("<details")

    def test_status_pills_and_appliances_folded_into_details(self, client_with_chat, store):
        setup_passphrase(client_with_chat)  # 隐私表单在场，才能断言其在折叠区之外
        store.upsert_appliance(
            ApplianceModel(
                name="取暖器",
                aliases=("取暖器",),
                actions={"打开": ("打开",)},
                codes={},
                is_high_risk=True,
                enabled=False,
            )
        )
        body = client_with_chat.get("/").text
        folded = _details_block(body)
        assert "设备状态与更多" in folded, "折叠区摘要行"
        assert "隐私模式：" in folded and "网络：" in folded and "远程服务：" in folded
        assert "取暖器" in folded, "家电只读表收纳进折叠区"
        assert 'action="/privacy"' not in folded, "隐私开关不进折叠区"
        assert 'action="/volume"' not in folded and 'action="/speed"' not in folded

    def test_details_collapsed_by_default(self, client_with_chat):
        body = client_with_chat.get("/").text
        assert "<details" in body and "</details>" in body
        assert re.search(r"<details\b[^>]*\bopen\b", body) is None, "默认收起"

    def test_appliance_empty_state_kept_in_details(self, client_with_chat):
        body = client_with_chat.get("/").text
        assert "还没有配置家电。请家人在管理后台添加。" in _details_block(body)


# ---------- 首页改版：聊天入口可用性（U5） ----------


class TestChatEntryAvailability:
    def test_entry_disabled_not_hidden_when_chat_missing(self, client):
        """chat=None：同位置显式"暂不可用"，不可点、不隐藏、不报错。"""
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.text
        assert 'href="/chat"' not in body, "未装配时入口不可点"
        assert "和小丽拉聊天" in body, "入口位置保留，不隐藏"
        assert "暂不可用" in body

    def test_entry_links_to_picker_when_chat_assembled(self, client_with_chat):
        body = client_with_chat.get("/").text
        assert 'href="/chat"' in body
        assert "暂不可用" not in body


# ---------- 首页改版：开发者控制台链接（U5 折叠收纳） ----------


class TestDevConsoleLink:
    def test_no_dev_link_when_disabled(self, client):
        body = client.get("/").text
        assert "开发者控制台" not in body
        assert 'href="/dev"' not in body

    def test_dev_link_inside_details_when_enabled(self, services, store):
        services.dev = _DevStub(store)
        body = TestClient(create_app(services)).get("/").text
        assert 'href="/dev"' in body
        assert 'href="/dev"' in _details_block(body), "dev 链接收纳进折叠区"


# ---------- 首页改版：音量 / 语速步进快捷控件（R2，U5） ----------


class TestQuickSteppers:
    def test_minus_plus_forms_post_existing_endpoints(self, client_with_chat):
        """−/+ 各自小 form POST 既有端点与参数名；不新增端点。"""
        body = client_with_chat.get("/").text
        assert body.count('action="/volume"') == 2
        assert body.count('action="/speed"') == 2
        assert body.count('name="value"') == 4

    def test_step_values_rendered_from_current(self, client_with_chat):
        """默认 1.0：音量/语速步进目标 0.9 / 1.1（step 0.1）。"""
        body = client_with_chat.get("/").text
        assert 'value="0.9"' in body
        assert 'value="1.1"' in body

    def test_volume_step_post_takes_effect_and_persists(self, client_with_chat, services):
        resp = client_with_chat.post(
            "/volume", data={"value": "1.5"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert services.settings.volume == 1.5
        body = client_with_chat.get("/").text
        assert 'value="1.4"' in body and 'value="1.6"' in body, "下一档按新值步进"

    def test_volume_stepper_clamped_to_allowed_range(self, client_with_chat, services):
        services.settings.set_volume(2.0)
        body = client_with_chat.get("/").text
        assert 'value="2.1"' not in body, "上界 2.0 内提交（越界交给服务端拒绝）"
        services.settings.set_volume(0.1)
        body = client_with_chat.get("/").text
        assert 'value="0.0"' not in body, "下界 0.1 内提交"

    def test_speed_stepper_clamped_to_allowed_range(self, client_with_chat, services):
        services.settings.set_tts_speed(2.0)
        body = client_with_chat.get("/").text
        assert 'value="2.1"' not in body
        services.settings.set_tts_speed(0.5)
        body = client_with_chat.get("/").text
        assert 'value="0.4"' not in body

    def test_out_of_range_still_rejected_server_side(self, client_with_chat, services):
        """前端钳制不替代服务端校验：越界值提交后错误路径照常 400。"""
        resp = client_with_chat.post("/volume", data={"value": "9.9"})
        assert resp.status_code == 400
        assert "音量数值无效" in resp.text
        assert services.settings.volume == 1.0
