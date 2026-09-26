"""U8 后台测试夹具。

路径纪律：backend 自身以 `app` 包导入；设备端包（`lira`）经 sys.path 注入
`device/` 目录，供集成测试直接使用**真实的** `lira.sync.SyncClient` 与
`lira.appliances.store.ApplianceStore`（协议 schema 单一来源复用）。
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT / "backend"), str(REPO_ROOT / "device")):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.auth import SESSION_COOKIE  # noqa: E402
from app.main import create_app  # noqa: E402
from app.protocol import HelloMsg  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    application = create_app(tmp_path / "backend.db")
    yield application
    application.state.db.close()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin(client):
    """已完成首启设置并登录的管理员客户端。"""
    r = client.post("/setup", data={"username": "parent", "password": "password123"})
    assert r.status_code == 200, "首启设置应成功"
    return client


def extract_csrf(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert m, "页面应内嵌 CSRF token"
    return m.group(1)


def apost(client, url: str, data: dict | None = None, follow_redirects: bool = False, **fields):
    """带 CSRF 的管理端 POST（默认不跟随重定向，便于断言 303）。"""
    payload = dict(data or {})
    payload.update(fields)
    payload["_csrf"] = extract_csrf(client)
    return client.post(url, data=payload, follow_redirects=follow_redirects)


@pytest.fixture()
def device_token(admin):
    """经 API 注册一台设备并返回一次性 token（同时返回客户端）。"""
    r = apost(admin, "/admin/devices", name="客厅设备")
    assert r.status_code == 303
    from urllib.parse import urlparse, parse_qs

    qs = parse_qs(urlparse(r.headers["location"]).query)
    return qs["token_once"][0]


@contextmanager
def ws_device_session(client, token: str):
    """模拟设备 WS 角色：首帧 hello 出示 token → auth_ok。"""
    with client.websocket_connect("/ws/device") as ws:
        ws.send_json(HelloMsg(token=token).to_json())
        reply = ws.receive_json()
        assert reply == {"type": "auth_ok"}, reply
        yield ws


class WSAdapter:
    """TestClient WS 会话 → lira.sync.SyncTransport 适配（真实设备端参与集成）。"""

    def __init__(self, session):
        self._session = session

    async def send(self, frame):
        self._session.send_json(frame)

    async def receive(self):
        return self._session.receive_json()

    async def close(self):
        self._session.close()
