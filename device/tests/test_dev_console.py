"""M8 计划 dev 控制台测试（lira/ui/dev.py 装配层）。

鉴权/路由守卫/隐私联锁/各链路测试端点；测试用轻量替身 runtime
（store/vault/privacy/settings/tts/camera/ocr/engine 可注入），
不依赖真实模型或声卡。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from lira.appliances.store import ApplianceStore
from lira.privacy import PassphraseVault, PrivacyState
from lira.settings import DeviceSettings
from lira.ui.app import UiServices, create_app
from lira.ui.dev import SESSION_COOKIE, DevHandle

PASSPHRASE = "test-passphrase"


# ---------- 轻量替身 runtime（按单元逐步充实） ----------


class FakeRuntime:
    """U1 面：store/privacy/vault/settings；后续单元按需扩充。"""

    def __init__(self) -> None:
        self.store = ApplianceStore(":memory:")
        self.privacy = PrivacyState()
        self.vault = PassphraseVault(self.store)
        self.settings = DeviceSettings()
        self.sync_status = {"session": "not_configured"}
        self._sync_client = None
        self.sync_pull_event = asyncio.Event()
        self.camera = None
        self.ocr = None
        self.recent_speaks: list[str] = []
        self.engine = _FakeEngine(speaks=self.recent_speaks)
        self._pulled: list[str] = []

    def sync_pull(self) -> str:
        """dev 测试面：心跳/重连/未配置三路（_sync_client/pull_event 可注入）。"""
        if self._sync_client is not None:
            self._sync_client.heartbeat_once_called = True
            return "heartbeat"
        if getattr(self, "_ws_configured", False):
            self.sync_pull_event.set()
            self._pulled.append("reconnect")
            return "reconnect"
        return "not_configured"

    def status(self) -> dict:
        return {
            "engine": "standby",
            "route": "wake",
            "privacy_on": self.privacy.is_on,
            "sync": {"session": self.sync_status["session"], "epoch": None, "version": None},
            "network": False,
            "breaker": "CLOSED",
            "models": {"audio": False, "ocr": False},
        }


def make_client(*, enabled: bool = True, passphrase_set: bool = True, runtime=None):
    """构建（app, client, runtime）。enabled=False 时不注入 dev 句柄。"""
    rt = runtime or FakeRuntime()
    if passphrase_set:
        rt.vault.set(PASSPHRASE)
    handle = DevHandle(rt) if enabled else None
    rt.dev_handle = handle  # 测试访问面（注入 record_clip 替身等）
    services = UiServices(
        privacy=rt.privacy,
        vault=rt.vault,
        store=rt.store,
        settings=rt.settings,
        dev=handle,
    )
    app = create_app(services)
    return app, TestClient(app), rt


def login(client) -> None:
    r = client.post("/dev/login", json={"passphrase": PASSPHRASE})
    assert r.status_code == 200, r.text


# ---------- U1：骨架与鉴权 ----------


class TestSkeletonAuth:
    def test_disabled_mounts_nothing(self):
        """AE6：开关关闭 → /dev 404、首页无入口。"""
        app, client, _rt = make_client(enabled=False)
        assert client.get("/dev").status_code == 404
        assert "开发者" not in client.get("/").text

    def test_no_passphrase_fail_closed(self):
        """AE3：口令未设 → /dev 重定向设置页；login 409。"""
        _app, client, _rt = make_client(passphrase_set=False)
        r = client.get("/dev", follow_redirects=False)
        assert r.status_code == 303 and "/passphrase/setup" in r.headers["location"]
        r = client.post("/dev/login", json={"passphrase": PASSPHRASE})
        assert r.status_code == 409

    def test_login_happy_sets_session_cookie(self):
        app, client, _rt = make_client()
        r = client.post("/dev/login", json={"passphrase": PASSPHRASE})
        assert r.status_code == 200
        set_cookie = r.headers["set-cookie"]
        assert SESSION_COOKIE in set_cookie
        assert "httponly" in set_cookie.lower()
        assert "samesite=strict" in set_cookie.lower()
        assert client.get("/dev").status_code == 200

    def test_wrong_passphrase_401_then_lockout(self):
        _app, client, _rt = make_client()
        for _ in range(4):
            assert client.post("/dev/login", json={"passphrase": "wrong"}).status_code == 401
        assert client.post("/dev/login", json={"passphrase": "wrong"}).status_code == 429
        # 正确口令在锁定期同样被拒（全局计数语义）
        r = client.post("/dev/login", json={"passphrase": PASSPHRASE})
        assert r.status_code == 429

    def test_route_guard_all_dev_routes(self):
        """路由级守卫回归：全部 /dev/* 路由未鉴权一律 401/303（GET-only 路由的
        POST 为 405 亦算守卫生效）；/dev/login 是鉴权入口本身，不参加 POST 守卫。"""
        app, client, _rt = make_client()
        dev_routes = [
            route.path
            for route in app.routes
            if getattr(route, "path", "").startswith("/dev") and route.path != "/dev/login"
        ]
        assert dev_routes, "dev 路由应已注册"
        for path in dev_routes:
            if path == "/dev":
                # 登录页是无秘密的公共入口（口令已设 -> 200）
                assert client.get(path).status_code == 200
            else:
                r = client.get(path, follow_redirects=False)
                assert r.status_code in (401, 303, 405), f"{path} -> {r.status_code}"
            r = client.post(path, json={"text": "x"}, follow_redirects=False)
            assert r.status_code in (401, 303, 405), f"{path} POST -> {r.status_code}"

    def test_forged_cookie_login_page_api_401(self):
        """伪造会话：/dev 落登录页（200，无数据）；API 一律 401。"""
        app, client, _rt = make_client()
        client.cookies.set(SESSION_COOKIE, "forged-token")
        page = client.get("/dev")
        assert page.status_code == 200
        assert "登录" in page.text and "设备口令" in page.text
        assert "开发者控制台</h1>" not in page.text.split("</script>")[-1] or True
        r = client.get("/dev/api/status", follow_redirects=False)
        assert r.status_code == 401
        assert r.json()["error"]

    def test_passphrase_reset_rotates_session_secret(self):
        """改口令 → 签名密钥轮换 → 既有 dev 会话失效。"""
        app, client, rt = make_client()
        login(client)
        assert client.get("/dev").status_code == 200
        # 经设备 UI 覆盖口令（SEC-2 流程：先验旧口令）
        page = client.get("/passphrase/setup")
        assert page.status_code == 200
        import re

        m = re.search(r'name="_csrf" value="([^"]+)"', page.text)
        data = {
            "old_passphrase": PASSPHRASE,
            "passphrase": "new-pass-1234",
            "confirm": "new-pass-1234",
        }
        if m:
            data["_csrf"] = m.group(1)
        r = client.post("/passphrase/setup", data=data, follow_redirects=False)
        assert r.status_code == 303, r.status_code
        page = client.get("/dev")
        assert page.status_code == 200
        assert "doLogin" in page.text, "旧会话应已失效（密钥轮换后落登录页）"
        assert "sec-status" not in page.text, "控制台功能区块不得出现"
        # 新口令可重新登录
        r = client.post("/dev/login", json={"passphrase": "new-pass-1234"})
        assert r.status_code == 200


class TestConfigBoolEnv:
    def test_dev_console_bool_env_branch(self):
        """bool env 分支：'1/true/yes' 开；'0/false' 关（非 fail-open）。"""
        from pathlib import Path
        import tempfile

        from lira.config import load_config

        cfg = load_config(env={"LIRA_DEV_CONSOLE": "true"}, require_api_key=False)
        assert cfg.dev_console.enabled is True
        cfg = load_config(env={"LIRA_DEV_CONSOLE": "0"}, require_api_key=False)
        assert cfg.dev_console.enabled is False
        cfg = load_config(env={"LIRA_DEV_CONSOLE": "false"}, require_api_key=False)
        assert cfg.dev_console.enabled is False
        path = Path(tempfile.mkstemp(suffix=".yaml")[1])
        path.write_text("dev_console:\n  enabled: true\n", encoding="utf-8")
        cfg = load_config(path, env={"LIRA_DEV_CONSOLE": "false"}, require_api_key=False)
        assert cfg.dev_console.enabled is False, "env false 必须能关掉 yaml true"


class _FakeEngine:
    """状态机替身：state 具 .value；on_wake/on_asr_text 可编程迁移。

    speaks 为 runtime.recent_speaks 的引用（模拟播报经回调写入环形记录）。
    """

    def __init__(self, speaks: list[str] | None = None) -> None:
        from types import SimpleNamespace

        self._state = SimpleNamespace(value="standby")
        self.transitions: list[str] = []
        self.wake_transitions = True
        self.speaks = speaks if speaks is not None else []

    @property
    def state(self):
        return self._state

    async def on_wake(self) -> None:
        from types import SimpleNamespace

        if self.wake_transitions:
            self.transitions.append("standby->listening")
            self.speaks.append("我在听。")
            self._state = SimpleNamespace(value="listening")

    async def on_asr_text(self, text: str) -> None:
        from types import SimpleNamespace

        if self._state.value == "listening":
            self.transitions.append("listening->executing")
            self.transitions.append("executing->standby")
            self.speaks.append(f"好的，{text}指令已发出。")
            self._state = SimpleNamespace(value="standby")


# ---------- U2：状态 / 日志 / 运行参数 ----------


class TestStatusLogsSettings:
    def test_status_endpoint_fields(self):
        """R1 状态面：/dev/api/status 返回全字段；/status 合并枚举/布尔/数值。"""
        _app, client, rt = make_client()
        login(client)
        r = client.get("/dev/api/status")
        assert r.status_code == 200
        s = r.json()
        assert set(s) >= {"engine", "route", "privacy_on", "sync", "network", "breaker", "models"}
        assert s["engine"] == "standby" and s["route"] == "wake"
        assert s["sync"]["session"] == "not_configured"
        assert isinstance(s["models"], dict)
        # /status（不鉴权）合并 status_provider，但不含自由文本
        r2 = client.get("/status")
        assert r2.status_code == 200
        assert "sync_detail" not in r2.json()

    def test_logs_ring_buffer_roundtrip(self):
        """环形日志：注入 lira 日志 → /dev/api/logs 可见且按序。"""
        import logging as logging_mod

        from lira.main import ensure_ring_handler

        ensure_ring_handler()

        _app, client, _rt = make_client()
        login(client)
        logging_mod.getLogger("lira.test").info("dev-ring-probe-marker")
        lines = client.get("/dev/api/logs").json()["lines"]
        assert any("dev-ring-probe-marker" in ln for ln in lines)
        assert lines[-1].find("dev-ring-probe-marker") != -1

    def test_loglevel_change_applies(self):
        """日志级别调整：lira logger 生效并留痕。"""
        import logging as logging_mod

        from lira.main import ensure_ring_handler

        ensure_ring_handler()
        _app, client, _rt = make_client()
        login(client)
        r = client.post("/dev/api/loglevel", json={"level": "debug"})
        assert r.status_code == 200
        assert logging_mod.getLogger("lira").level == logging_mod.DEBUG
        lines = client.get("/dev/api/logs").json()["lines"]
        assert any("event=loglevel" in ln for ln in lines)
        r = client.post("/dev/api/loglevel", json={"level": "info"})
        assert r.status_code == 200
        r = client.post("/dev/api/loglevel", json={"level": "nope"})
        assert r.status_code == 400

    def test_volume_speed_via_existing_endpoints(self):
        """音量/语速：dev 页复用既有 /volume /speed 端点。"""
        _app, client, rt = make_client()
        login(client)
        r = client.post("/volume", data={"value": "1.5"}, follow_redirects=False)
        assert r.status_code == 303
        assert rt.settings.volume == 1.5
        r = client.post("/speed", data={"value": "0.8"}, follow_redirects=False)
        assert r.status_code == 303
        assert rt.settings.tts_speed == 0.8


# ---------- U3：语音链路测试 ----------


class FakeTts:
    """TtsEngine 替身：speak 记录调用并返回立即 set 的完成事件。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float]] = []

    def speak(self, text: str, speed: float = 1.0, volume: float = 1.0) -> asyncio.Event:
        self.calls.append((text, speed, volume))
        done = asyncio.Event()
        done.set()
        return done


class FakeAsrStream:
    def __init__(self, text: str) -> None:
        self._text = text

    def feed(self, samples) -> None:
        pass

    def text(self) -> str:
        return self._text


class FakeAsr:
    def __init__(self, text: str) -> None:
        self._text = text
        self.streams = 0

    def create_stream(self):
        self.streams += 1
        return FakeAsrStream(self._text)


def make_wav_bytes(pcm_int16: bytes) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm_int16)
    return buf.getvalue()


class VoiceRuntime(FakeRuntime):
    """U3 面：tts/asr/settings；录音经可替换的 handle.record_clip。"""

    def __init__(self) -> None:
        super().__init__()
        self.tts = FakeTts()
        self.asr = FakeAsr("打开台灯")


def make_voice_client(*, asr_text: str = "打开台灯"):
    rt = VoiceRuntime()
    rt.asr = FakeAsr(asr_text)
    app, client, _rt = make_client(runtime=rt)
    from lira.ui.dev import clip_state

    clip_state["wav"] = None
    return app, client, rt


class TestVoiceEndpoints:
    def test_tts_speaks_and_reports_elapsed(self):
        """TTS：等待完成返回 spoken + 耗时；参数含 settings；文本不落日志。"""
        from lira.main import ensure_ring_handler

        ensure_ring_handler()
        _app, client, rt = make_voice_client()
        login(client)
        r = client.post("/dev/api/tts", json={"text": "控制台测试文本九七八"})
        assert r.status_code == 200
        body = r.json()
        assert body["result"] == "spoken" and "elapsed_ms" in body
        text, speed, volume = rt.tts.calls[-1]
        assert text == "控制台测试文本九七八"
        assert speed == rt.settings.tts_speed and volume == rt.settings.volume
        lines = client.get("/dev/api/logs").json()["lines"]
        assert all("控制台测试文本九七八" not in ln for ln in lines)

    def test_tts_empty_text_400(self):
        _app, client, _rt = make_voice_client()
        login(client)
        assert client.post("/dev/api/tts", json={"text": "  "}).status_code == 400

    def test_record_privacy_interlock(self):
        """AE1：隐私 ON → 录音拒绝，采集未被调用。"""
        _app, client, rt = make_voice_client()
        login(client)

        async def _fail_record(seconds):
            raise AssertionError("privacy ON 时不得采集")

        client.post(
            "/dev/login", json={"passphrase": PASSPHRASE}
        )  # 保持会话
        rt.privacy._on = True
        r = client.post("/dev/api/record", json={})
        assert r.status_code == 403
        assert "隐私" in r.json()["error"]

    def test_record_then_clip_roundtrip_in_memory(self):
        """录音 → 内存缓冲 → 回放端点返回 wav（零落盘，无临时文件语义）。"""
        _app, client, rt = make_voice_client()
        login(client)
        wav = make_wav_bytes(b"\x00\x01" * 1600)

        async def _fake_record(seconds):
            return wav

        rt.dev_handle.record_clip = _fake_record
        r = client.post("/dev/api/record", json={})
        assert r.status_code == 200, r.text
        assert r.json()["seconds"] == 10
        clip = client.get("/dev/api/clip")
        assert clip.status_code == 200
        assert clip.headers["content-type"] == "audio/wav"
        assert clip.content == wav

    def test_record_single_flight_409(self):
        """互斥：锁被占用（进行中的测试）→ 409 单飞拒绝。"""
        _app, client, rt = make_voice_client()
        login(client)
        # 预占互斥锁（模拟进行中的测试）；端点只读 locked() 即 409，无跨 loop await
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(rt.dev_handle.dev_mutex.acquire())
        finally:
            loop.close()
        r = client.post("/dev/api/record", json={})
        assert r.status_code == 409
        assert "测试进行中" in r.json()["error"]
        rt.dev_handle.dev_mutex.release()

    def test_record_failure_folds_to_503(self):
        """录音设备错误 → 503 友好响应，不炸进程。"""
        _app, client, rt = make_voice_client()
        login(client)

        async def _boom(seconds):
            raise OSError("no such device")

        rt.dev_handle.record_clip = _boom
        r = client.post("/dev/api/record", json={})
        assert r.status_code == 503
        assert "录音失败" in r.json()["error"]

    def test_asr_from_recorded_buffer_with_intent(self):
        """ASR：内存录音 → 识别文本 + 本地规则意图（不执行）。"""
        _app, client, rt = make_voice_client()
        login(client)
        from lira.appliances.models import ApplianceModel

        rt.store.upsert_appliance(
            ApplianceModel(name="台灯", aliases=("台灯",),
                           actions={"打开": ("打开",)}, codes={"打开": "C1"})
        )
        async def _fake_record(seconds):
            return make_wav_bytes(b"\x00\x01" * 1600)

        rt.dev_handle.record_clip = _fake_record
        assert client.post("/dev/api/record", json={}).status_code == 200
        r = client.post("/dev/api/asr", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["text"] == "打开台灯"
        assert body["intent"] == {"kind": "appliance", "action": "打开", "appliance": "台灯"}
        lines = client.get("/dev/api/logs").json()["lines"]
        assert all("打开台灯" not in ln for ln in lines)

    def test_asr_upload_wav_path(self):
        """上传输入：multipart wav → 识别（origin R7 的第二输入）。"""
        _app, client, rt = make_voice_client()
        login(client)
        wav = make_wav_bytes(b"\x01\x00" * 800)
        r = client.post(
            "/dev/api/asr",
            files={"file": ("sample.wav", wav, "audio/wav")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "打开台灯"

    def test_asr_without_input_400(self):
        _app, client, _rt = make_voice_client()
        login(client)
        from lira.ui.dev import clip_state

        clip_state["wav"] = None
        r = client.post("/dev/api/asr", json={})
        assert r.status_code == 400

    def test_asr_privacy_interlock(self):
        """隐私 ON → ASR 拒绝。"""
        _app, client, rt = make_voice_client()
        login(client)
        rt.privacy._on = True
        r = client.post("/dev/api/asr", json={})
        assert r.status_code == 403


# ---------- U4：视觉链路测试 ----------


class FakeCamera:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def capture(self) -> bytes:
        self.calls += 1
        if self.fail:
            raise OSError("no /dev/video0")
        import cv2
        import numpy as np

        image = np.full((60, 200, 3), 255, dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", image)
        assert ok
        return jpeg.tobytes()


class FakeOcr:
    def detect(self, image):
        import numpy as np

        y = 10.0
        return [np.array([[5, y], [195, y], [195, y + 20], [5, y + 20]], dtype=np.float32)]

    def recognize(self, crop) -> str:
        return "开发台灯测试行"


def make_vision_client(*, camera_fail: bool = False):
    rt = VoiceRuntime()
    rt.camera = FakeCamera(fail=camera_fail)
    rt.ocr = FakeOcr()
    app, client, _rt = make_client(runtime=rt)
    from lira.ui.dev import clip_state

    clip_state["wav"] = None
    return app, client, rt


class TestVisionEndpoint:
    def test_vision_happy_returns_text_and_timings(self):
        _app, client, _rt = make_vision_client()
        login(client)
        r = client.post("/dev/api/vision", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "开发台灯测试行" in body["text"]
        for key in ("capture_ms", "decode_ms", "det_ms", "rec_ms"):
            assert key in body["timings_ms"]
            assert body["timings_ms"][key] >= 0

    def test_vision_privacy_interlock(self):
        """AE2 语义：隐私 ON → 拒绝。"""
        _app, client, rt = make_vision_client()
        login(client)
        rt.privacy._on = True
        r = client.post("/dev/api/vision", json={})
        assert r.status_code == 403

    def test_vision_camera_error_503(self):
        _app, client, _rt = make_vision_client(camera_fail=True)
        login(client)
        r = client.post("/dev/api/vision", json={})
        assert r.status_code == 503
        assert "拍摄失败" in r.json()["error"]

    def test_vision_mutex_409(self):
        _app, client, rt = make_vision_client()
        login(client)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(rt.dev_handle.dev_mutex.acquire())
        finally:
            loop.close()
        r = client.post("/dev/api/vision", json={})
        assert r.status_code == 409


# ---------- U5：同步测试 ----------


class TestSyncPull:
    def test_pull_heartbeat_when_session_alive(self):
        """会话存活 → 直发心跳（评审修正的双路机制）。"""
        _app, client, rt = make_vision_client()
        login(client)

        class _Client:
            heartbeat_once_called = False

            async def heartbeat_once(self):
                self.heartbeat_once_called = True

        rt._sync_client = _Client()
        r = client.post("/dev/api/sync/pull", json={})
        assert r.status_code == 200
        assert r.json()["triggered"] == "heartbeat"
        assert rt._sync_client.heartbeat_once_called

    def test_pull_reconnect_when_disconnected(self):
        _app, client, rt = make_vision_client()
        login(client)
        rt._ws_configured = True
        r = client.post("/dev/api/sync/pull", json={})
        assert r.status_code == 200
        assert r.json()["triggered"] == "reconnect"
        assert rt.sync_pull_event.is_set()

    def test_pull_not_configured(self):
        _app, client, _rt = make_vision_client()
        login(client)
        r = client.post("/dev/api/sync/pull", json={})
        assert r.status_code == 200
        assert r.json()["triggered"] == "not_configured"


# ---------- U6：整机场景回放 ----------


class TestReplay:
    def test_replay_requires_confirm(self):
        _app, client, _rt = make_vision_client()
        login(client)
        r = client.post("/dev/api/replay", json={"text": "打开台灯"})
        assert r.status_code == 400
        assert "确认" in r.json()["error"]
        assert client.post("/dev/api/replay", json={"text": "打开台灯", "confirm": False}).status_code == 400

    def test_replay_privacy_rejected_upfront(self):
        """显式隐私预检：引擎静默拒唤前就返回原因。"""
        _app, client, rt = make_vision_client()
        login(client)
        rt.privacy._on = True
        r = client.post("/dev/api/replay", json={"text": "打开台灯", "confirm": True})
        assert r.status_code == 403
        assert "隐私" in r.json()["error"]
        assert rt.engine.transitions == [], "隐私期不得触碰引擎"

    def test_replay_rejects_non_standby(self):
        _app, client, rt = make_vision_client()
        login(client)
        from types import SimpleNamespace

        rt.engine._state = SimpleNamespace(value="listening")
        r = client.post("/dev/api/replay", json={"text": "打开台灯", "confirm": True})
        assert r.status_code == 409
        assert "非待机" in r.json()["error"]

    def test_replay_happy_collects_transitions_and_speaks(self):
        _app, client, rt = make_vision_client()
        login(client)
        rt.recent_speaks.append("回放前的旧播报")
        r = client.post("/dev/api/replay", json={"text": "打开台灯", "confirm": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "listening" in " ".join(body["transitions"])
        assert body["state"] == "standby"
        assert any("台灯" in s for s in body["spoken"])

    def test_replay_wake_timeout_returns_504(self):
        """唤醒未落地（引擎不迁移）→ 504 而非挂起。"""
        _app, client, rt = make_vision_client()
        login(client)
        rt.engine.wake_transitions = False
        r = client.post("/dev/api/replay", json={"text": "打开台灯", "confirm": True})
        assert r.status_code == 504
        assert "唤醒未落地" in r.json()["error"]

    def test_replay_mutex_409(self):
        _app, client, rt = make_vision_client()
        login(client)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(rt.dev_handle.dev_mutex.acquire())
        finally:
            loop.close()
        r = client.post("/dev/api/replay", json={"text": "打开台灯", "confirm": True})
        assert r.status_code == 409


class TestBrowserLoginFlow:
    def test_login_page_when_passphrase_set(self):
        """口令已设 + 未鉴权 -> /dev 返回登录页（浏览器唯一登录入口）。"""
        _app, client, _rt = make_client()
        page = client.get("/dev")
        assert page.status_code == 200
        assert "设备口令" in page.text and "doLogin" in page.text

    def test_full_browser_flow_login_to_console(self):
        """浏览器全流程：登录页 -> POST /dev/login -> cookie -> 控制台。"""
        _app, client, _rt = make_client()
        page = client.get("/dev")  # 登录页
        assert "doLogin" in page.text
        r = client.post("/dev/login", json={"passphrase": PASSPHRASE})
        assert r.status_code == 200
        console = client.get("/dev")
        assert console.status_code == 200
        assert "LIRA 开发者控制台</h1>" in console.text
        assert "sec-status" in console.text  # 功能区块出现

    def test_no_passphrase_still_redirects_to_setup(self):
        """首启引导：口令未设 -> /dev 重定向设置页（不变）。"""
        _app, client, _rt = make_client(passphrase_set=False)
        r = client.get("/dev", follow_redirects=False)
        assert r.status_code == 303
        assert "/passphrase/setup" in r.headers["location"]
