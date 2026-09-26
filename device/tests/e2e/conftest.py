"""U9 e2e 夹具（全 mock 装配 fixture + 真实后台 TestClient 复用）。

路径纪律：设备端以 `lira` 包导入；后台以 `app` 包导入（backend/tests 同一
注入方式），两侧真实代码在同一个进程内联通（AE5/AE7/对抗用例）。
"""

from __future__ import annotations

import asyncio
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parents[2]
for p in (str(REPO_ROOT / "device"), str(REPO_ROOT / "backend"), str(E2E_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from harness import DeviceHarness  # noqa: E402

from lira.audio._paths import (  # noqa: E402
    DOWNLOAD_HINT,
    KEYWORDS_DIR,
    KWS_MODEL_DIR,
    ASR_MODEL_DIR,
)

_kws_ready = KWS_MODEL_DIR.is_dir() and (KEYWORDS_DIR / "wakeword_raw.txt").is_file()
_asr_ready = ASR_MODEL_DIR.is_dir()

#: 真实语音链路用例（AE1 唤醒无命中 / AE4 白名单 KWS）依赖 KWS 模型
requires_kws = pytest.mark.skipif(not _kws_ready, reason=f"KWS 模型/关键词未就绪。{DOWNLOAD_HINT}")
requires_asr = pytest.mark.skipif(not _asr_ready, reason=f"ASR 模型未就绪。{DOWNLOAD_HINT}")


# ---------- 语音引擎（module 级共享，流按 harness 独立创建） ----------

@pytest.fixture(scope="module")
def voices():
    """(唤醒 KWS, 流式 ASR, 白名单 KWS)；模型缺失时为 None（文本注入仍可用）。"""
    if not (_kws_ready and _asr_ready):
        yield None
        return
    from lira.audio.asr import StreamingAsr
    from lira.audio.kws import WakeWordKws
    from lira.dialog.intents import PLAYBACK_WHITELIST_KEYWORDS_FILE

    yield (
        WakeWordKws(),
        StreamingAsr(),
        WakeWordKws(keywords_file=PLAYBACK_WHITELIST_KEYWORDS_FILE),
    )


@pytest.fixture()
async def harness(tmp_path, voices):
    """整机 harness：全 mock 装配（离线纯本地；同步由用例按需 attach）。"""
    h = DeviceHarness(tmp_path / "device.db", voices,
                      ocr_text="布洛芬缓释胶囊\n用法用量：口服，一次一粒，一日两次\n"
                               "禁忌：对本品过敏者禁用。不良反应：偶见轻度胃肠不适。\n"
                               "请按说明书或在药师指导下服用。")
    h.start()
    yield h
    await h.aclose()


# ---------- 真实后台（复用 backend/tests 的夹具形态） ----------

@pytest.fixture()
def backend_app(tmp_path):
    from app.main import create_app

    application = create_app(tmp_path / "backend.db")
    yield application
    application.state.db.close()


@pytest.fixture()
def backend(backend_app):
    with TestClient(backend_app) as c:
        yield c


@pytest.fixture()
def admin(backend):
    """已完成首启设置并登录的管理员客户端。"""
    r = backend.post("/setup", data={"username": "parent", "password": "password123"})
    assert r.status_code == 200, "首启设置应成功"
    return backend


def extract_csrf(client) -> str:
    html = client.get("/").text
    if 'name="_csrf"' not in html and "登录" in html:
        # TestClient cookie jar 偶发丢失（portal 高负载下 ~1/10 全量运行，WSL2）：
        # 会话 JWT 本身 12h 有效、后台自身测试从不复现 → e2e 夹具自愈重登录一次。
        r = client.post("/login", data={"username": "parent", "password": "password123"})
        assert r.status_code in (200, 303), r.status_code
        html = client.get("/").text
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert m, f"页面应内嵌 CSRF token; head={html[:200]!r}"
    return m.group(1)


def apost(client, url: str, data: dict | None = None, **fields):
    """带 CSRF 的管理端 POST（不跟随重定向，便于断言 303）。"""
    payload = dict(data or {})
    payload.update(fields)
    payload["_csrf"] = extract_csrf(client)
    return client.post(url, data=payload, follow_redirects=False)


@pytest.fixture()
def device_token(admin):
    """经管理端 API 注册一台设备并返回一次性 token。"""
    r = apost(admin, "/admin/devices", name="客厅设备")
    assert r.status_code == 303
    return parse_qs(urlparse(r.headers["location"]).query)["token_once"][0]


@contextmanager
def ws_device_session(client, token: str):
    """模拟设备 WS 角色：首帧 hello 出示 token → auth_ok。"""
    from app.protocol import HelloMsg

    with client.websocket_connect("/ws/device") as ws:
        ws.send_json(HelloMsg(token=token).to_json())
        reply = ws.receive_json()
        assert reply == {"type": "auth_ok"}, reply
        yield ws


class WSAdapter:
    """TestClient WS 会话 → lira.sync.SyncTransport 适配（真实设备端参与）。"""

    def __init__(self, session):
        self._session = session

    async def send(self, frame):
        self._session.send_json(frame)

    async def receive(self):
        return self._session.receive_json()

    async def close(self):
        self._session.close()


class SyncPump:
    """设备端 SyncClient ↔ 真实后台 WS 的帧泵（AE5/AE7/对抗用例）。

    不用 run_forever（阻塞式 receive 不可超时取消），改为逐帧收→处理→回发，
    每个等待点都有后台侧**必然到达**的帧（首连推送 / 学习下发 / 快照推送），
    `run_until(pred)` 由此可以确定性等待谓词成立。
    """

    def __init__(self, sync, ws):
        self.sync = sync
        self.adapter = WSAdapter(ws)
        #: 设备实际收到的后台帧数（"多次离线变更只收最终快照"断言面）
        self.frames = 0

    async def run_until(self, pred, *, timeout: float = 5.0) -> None:
        if not self.sync.authenticated:
            await self.sync.connect()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not pred():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"同步泵等待条件超时: store={self.sync._store.current_epoch()}")
            frame = await asyncio.wait_for(self.adapter.receive(), timeout=remaining)
            self.frames += 1
            reply = await self.sync.handle_frame(frame)
            if reply is not None:
                await self.adapter.send(reply)

    async def connect(self) -> None:
        await self.sync.connect()
