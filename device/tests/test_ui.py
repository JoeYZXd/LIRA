"""U7 设备端触摸屏 UI 测试（R17/R12，fastapi TestClient + httpx）。

覆盖：
  - 状态卡/设备列表只读不鉴权；/status JSON
  - 口令首启设置流程（无默认值；不一致/过短 → 400；成功 → 入库哈希）
  - UI 隐私开关需口令：未设置口令 → 303 引导设置页（fail-closed）；
    错口令 → 401 且状态不变；对口令 → 殊途同归触碰同一 PrivacyState
  - 音量/语速设置（越界/非法 → 400）
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from lira.appliances.models import ApplianceModel
from lira.appliances.store import ApplianceStore
from lira.privacy import PassphraseVault, PrivacyState
from lira.ui.app import DeviceSettings, UiServices, create_app


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
