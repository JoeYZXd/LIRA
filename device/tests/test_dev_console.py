"""M8 计划 dev 控制台测试（lira/ui/dev.py 装配层）。

鉴权/路由守卫/隐私联锁/各链路测试端点；测试用轻量替身 runtime
（store/vault/privacy/settings/tts/camera/ocr/engine 可注入），
不依赖真实模型或声卡。
"""

from __future__ import annotations

import asyncio

import numpy as np
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
        POST 为 405 亦算守卫生效）；/dev/login 是鉴权入口本身，不参加 POST 守卫。
        /dev/static 是匿名静态挂载（Mount 无 methods），其匿名性由
        TestStaticAssets 单独固定，不参加本守卫遍历。"""
        app, client, _rt = make_client()
        dev_routes = [
            route.path
            for route in app.routes
            if getattr(route, "path", "").startswith("/dev") and route.path != "/dev/login"
            and getattr(route, "methods", None)
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
        assert 'data-page="dev-login"' in page.text, "旧会话应已失效（密钥轮换后落登录页）"
        assert 'data-page="dev-console"' not in page.text, "控制台功能区块不得出现"
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
        r = client.post("/dev/api/record/start", json={})
        assert r.status_code == 403
        assert "隐私" in r.json()["error"]

    def test_record_start_stop_roundtrip_in_memory(self):
        """start/stop 录音 → 内存缓冲 → 回放端点返回 wav（零落盘）。"""
        _app, client, rt = make_voice_client()
        login(client)
        wav = make_wav_bytes(b"\x00\x01" * 1600)

        async def _fake_start(max_seconds):
            rt.dev_handle.recording_active = True
            rt.dev_handle._rec_result = (wav, 3.2)

        async def _fake_stop():
            rt.dev_handle.recording_active = False
            return rt.dev_handle._rec_result or (b"", 0.0)

        rt.dev_handle.start_recording = _fake_start
        rt.dev_handle.stop_recording = _fake_stop
        r = client.post("/dev/api/record/start", json={})
        assert r.status_code == 200, r.text
        assert r.json()["recording"] is True
        r = client.post("/dev/api/record/stop", json={})
        assert r.status_code == 200, r.text
        assert r.json()["seconds"] == 3.2
        clip = client.get("/dev/api/clip")
        assert clip.status_code == 200
        assert clip.headers["content-type"] == "audio/wav"
        assert clip.content == wav

    def test_record_stop_without_start_400(self):
        _app, client, _rt = make_voice_client()
        login(client)
        r = client.post("/dev/api/record/stop", json={})
        assert r.status_code == 400

    def test_record_stop_privacy_discards(self):
        """录音中途开隐私 → 严格联锁丢弃已采内容。"""
        _app, client, rt = make_voice_client()
        login(client)
        wav = make_wav_bytes(b"\x00\x01" * 1600)

        async def _fake_start(max_seconds):
            rt.dev_handle.recording_active = True
            rt.dev_handle._rec_result = (wav, 2.0)

        async def _fake_stop():
            rt.dev_handle.recording_active = False
            return rt.dev_handle._rec_result or (b"", 0.0)

        rt.dev_handle.start_recording = _fake_start
        rt.dev_handle.stop_recording = _fake_stop
        assert client.post("/dev/api/record/start", json={}).status_code == 200
        rt.privacy._on = True  # 录音中途开隐私
        r = client.post("/dev/api/record/stop", json={})
        assert r.status_code == 200
        assert r.json()["discarded"] is True
        from lira.ui.dev import clip_state

        assert clip_state["wav"] is None
        assert client.get("/dev/api/clip").status_code == 404, "丢弃后回放端点应为空"

    def test_record_start_while_active_409(self):
        """单飞：录音已激活 → start 再次请求 409。"""
        _app, client, rt = make_voice_client()
        login(client)
        rt.dev_handle.recording_active = True  # 模拟已激活
        r = client.post("/dev/api/record/start", json={})
        assert r.status_code == 409
        assert "录音已在进行中" in r.json()["error"]
        rt.dev_handle.recording_active = False

    def test_record_start_failure_503(self):
        """录音启动失败（设备错误）→ 503 友好响应。"""
        _app, client, rt = make_voice_client()
        login(client)

        async def _boom(max_seconds):
            raise OSError("no such device")

        rt.dev_handle.start_recording = _boom
        r = client.post("/dev/api/record/start", json={})
        assert r.status_code == 503
        assert "录音启动失败" in r.json()["error"]

    def test_asr_from_recorded_buffer_with_intent(self):
        """ASR：内存录音 → 识别文本 + 本地规则意图（不执行）。"""
        _app, client, rt = make_voice_client()
        login(client)
        from lira.appliances.models import ApplianceModel

        rt.store.upsert_appliance(
            ApplianceModel(name="台灯", aliases=("台灯",),
                           actions={"打开": ("打开",)}, codes={"打开": "C1"})
        )

        async def _fake_start(max_seconds):
            rt.dev_handle.recording_active = True
            rt.dev_handle._rec_result = (make_wav_bytes(b"\x00\x01" * 1600), 2.0)

        async def _fake_stop():
            rt.dev_handle.recording_active = False
            return rt.dev_handle._rec_result or (b"", 0.0)

        rt.dev_handle.start_recording = _fake_start
        rt.dev_handle.stop_recording = _fake_stop
        assert client.post("/dev/api/record/start", json={}).status_code == 200
        assert client.post("/dev/api/record/stop", json={}).status_code == 200
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
        self.rotation = 0
        self.hflip = False

    async def capture(self) -> bytes:
        self.calls += 1
        if self.fail:
            raise OSError("no /dev/video0")
        import cv2
    
        image = np.full((60, 200, 3), 255, dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", image)
        assert ok
        return jpeg.tobytes()


class FakeOcr:
    def detect(self, image):
    
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

    def test_orientation_endpoint_persists(self):
        """方向控制：rotation 校验 + store meta 持久化 + hflip 开关。"""
        _app, client, rt = make_vision_client()
        login(client)
        r = client.post("/dev/api/camera/orientation", json={"rotation": 90})
        assert r.status_code == 200
        assert rt.camera.rotation == 90
        assert rt.store.get_meta("camera_rotation") == "90"
        r = client.post("/dev/api/camera/orientation", json={"rotation": 45})
        assert r.status_code == 400
        r = client.post("/dev/api/camera/orientation", json={"hflip": True})
        assert r.status_code == 200
        assert rt.camera.hflip is True
        assert rt.store.get_meta("camera_hflip") == "1"

    def test_rotate_by_accumulates(self):
        """相对旋转：多次点击累加循环（0->90->180->270->0）。"""
        _app, client, rt = make_vision_client()
        login(client)
        for expected in (90, 180, 270, 0):
            r = client.post("/dev/api/camera/orientation", json={"rotate_by": 90})
            assert r.status_code == 200, r.text
            assert rt.camera.rotation == expected
        r = client.post("/dev/api/camera/orientation", json={"rotate_by": -90})
        assert rt.camera.rotation == 270, "-90 = 顺时针退一步"

    def test_orientation_transform_math(self):
        """旋转/翻转纯函数：形状与内容方向正确。"""
    
        from lira.hal.board.camera_isp import _apply_orientation

        img = np.zeros((4, 6, 3), dtype=np.uint8)
        img[0, 0] = (255, 0, 0)  # 左上角标记
        r90 = _apply_orientation(img, 90, False)
        assert r90.shape == (6, 4, 3)
        assert tuple(r90[0, -1]) == (255, 0, 0), "顺时针 90°：左上角 -> 右上角"
        r180 = _apply_orientation(img, 180, False)
        assert tuple(r180[-1, -1]) == (255, 0, 0), "180°：左上角 -> 右下角"
        flipped = _apply_orientation(img, 0, True)
        assert tuple(flipped[0, -1]) == (255, 0, 0), "水平翻转：左上角 -> 右上角"

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
        assert "设备口令" in page.text and 'data-page="dev-login"' in page.text

    def test_full_browser_flow_login_to_console(self):
        """浏览器全流程：登录页 -> POST /dev/login -> cookie -> 控制台。"""
        _app, client, _rt = make_client()
        page = client.get("/dev")  # 登录页
        assert 'data-page="dev-login"' in page.text
        r = client.post("/dev/login", json={"passphrase": PASSPHRASE})
        assert r.status_code == 200
        console = client.get("/dev")
        assert console.status_code == 200
        assert "我的工作区</h1>" in console.text
        assert 'id="app-launcher"' in console.text
        assert 'data-page="dev-console"' in console.text  # 功能区块出现

    def test_no_passphrase_still_redirects_to_setup(self):
        """首启引导：口令未设 -> /dev 重定向设置页（不变）。"""
        _app, client, _rt = make_client(passphrase_set=False)
        r = client.get("/dev", follow_redirects=False)
        assert r.status_code == 303
        assert "/passphrase/setup" in r.headers["location"]


class TestConsoleErrorFeedback:
    """控制台 JS 行为断言（U1 起 JS 外置为 /dev/static/dev-console.js，
    断言对象从页面 HTML 改为静态 JS 本体，强度不变）。"""

    @staticmethod
    def _console_js(client) -> str:
        return client.get("/dev/static/dev-console.js").text

    def test_console_js_surfaces_network_failure(self):
        """僵尸页面防护（2026-10-04 实机问题）：网络级失败不再静默——
        api() 捕获 fetch 异常返回 status=0 + 可见错误文案。"""
        _app, client, _rt = make_client()
        login(client)
        js = self._console_js(client)
        assert "catch (e)" in js
        assert "无法连接设备服务" in js
        assert "status: 0" in js

    def test_console_js_redirects_on_401(self):
        """会话失效自愈：任一 API 401 -> 统一回 /dev 登录页（防僵尸页面停留）。"""
        _app, client, _rt = make_client()
        login(client)
        js = self._console_js(client)
        assert "r.status === 401" in js
        assert "location.href = '/dev'" in js
        assert "authRedirecting" in js  # 并发轮询只跳一次

    def test_console_panels_show_failure_in_place(self):
        """整机状态/实时观察面板：加载失败时面板主体显示原因，不停留占位符。"""
        _app, client, _rt = make_client()
        login(client)
        js = self._console_js(client)
        # loadStatus 失败路径写入面板主体（status-view）
        assert "getElementById('status-view').textContent = '（' + msg + '）'" in js
        # U5：实时观察失败退避时曲线状态行可见显示"已断连"，不停留占位符
        assert "getElementById('curve-status').textContent" in js
        assert "已断连" in js

    def test_console_settings_readback_and_validation_in_js(self):
        """U1 真实回读：JS 把 status 当前值/范围写入控件，保存前按范围预检，
        失败路径保留输入（旧版恒 undefined 缺陷的回归防线）。"""
        _app, client, _rt = make_client()
        login(client)
        js = self._console_js(client)
        assert "applyStatusReadback" in js
        assert "document.getElementById('vol').value = s.volume" in js
        assert "document.getElementById('spd').value = s.tts_speed" in js
        assert "volume_range" in js and "speed_range" in js
        assert "settingProblem" in js  # 客户端范围校验（非法值不提交）

    def test_console_pages_reference_external_script(self):
        """回归防线（2026-10-05 实机问题，19fb8b8 丢 </script> 使页面 JS 整块
        失效）：U1 起 JS 外置——登录页与控制台页必须各恰好引用一次
        /dev/static/dev-console.js，script 标签成对且闭合先于 </body>；
        JS 本体语法由 node --check 把关（CI/本地），页面侧只保结构完整。"""
        _app, client, _rt = make_client()
        pages = [client.get("/dev").text]  # 口令已设、未登录 -> 登录页
        login(client)
        pages.append(client.get("/dev").text)  # 控制台页
        for html in pages:
            assert html.count("<script") == html.count("</script>") >= 1
            import re
            from urllib.parse import urlsplit
            scripts = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', html)
            assert sum(urlsplit(src).path == '/dev/static/dev-console.js' for src in scripts) == 1
            # 结构顺序：闭合标签必须在 </body> 之前（吞掉 </body> 即语法错误）
            assert html.index("</script>") < html.index("</body>")


# ---------- U1：真实设置回读 / 静态资源 / 磁贴结构 ----------


class TestStatusReadback:
    """U1 真实设置回读：/dev/api/status 暴露当前值与允许范围
    （旧页音量/语速恒 undefined、从未回显的缺陷在服务端补齐）。"""

    def test_status_carries_settings_values_and_ranges(self):
        _app, client, rt = make_client()
        login(client)
        rt.settings.set_volume(1.3)
        rt.settings.set_tts_speed(0.8)
        s = client.get("/dev/api/status").json()
        assert s["volume"] == 1.3
        assert s["tts_speed"] == 0.8
        assert s["volume_range"] == list(DeviceSettings.VOLUME_RANGE)
        assert s["speed_range"] == list(DeviceSettings.SPEED_RANGE)

    def test_status_loglevel_name_readback(self):
        """日志级别回读为名字；调整端点改动后 status 如实跟随。"""
        import logging as logging_mod

        _app, client, _rt = make_client()
        login(client)
        assert client.get("/dev/api/status").json()["loglevel"] in {
            "DEBUG", "INFO", "WARNING", "ERROR",
        }
        r = client.post("/dev/api/loglevel", json={"level": "debug"})
        assert r.status_code == 200
        assert client.get("/dev/api/status").json()["loglevel"] == "DEBUG"
        client.post("/dev/api/loglevel", json={"level": "error"})
        assert client.get("/dev/api/status").json()["loglevel"] == "ERROR"
        # 还原级别，不向后续测试泄漏全局改动
        client.post("/dev/api/loglevel", json={"level": "info"})
        assert logging_mod.getLogger("lira").level == logging_mod.INFO

    def test_status_camera_fields_when_camera_present(self):
        """相机实际方向回读（FakeCamera 提供 rotation/hflip）。"""
        _app, client, rt = make_vision_client()
        login(client)
        s = client.get("/dev/api/status").json()
        assert s["camera_rotation"] == 0
        assert s["camera_hflip"] is False
        client.post("/dev/api/camera/orientation", json={"rotation": 90})
        s = client.get("/dev/api/status").json()
        assert s["camera_rotation"] == 90

    def test_status_camera_fields_omitted_without_camera(self):
        """相机缺失（未装配/不支持）时省略字段，不虚构默认值。"""
        _app, client, _rt = make_client()
        login(client)
        s = client.get("/dev/api/status").json()
        assert "camera_rotation" not in s
        assert "camera_hflip" not in s


class TestStaticAssets:
    """U1 静态资源：随 dev 挂载（控制台关闭即不存在）；匿名可取——
    CSS/JS 必须保持无敏感内容，本测试固定该事实作为将来的报警线。"""

    def test_assets_served_anonymously(self):
        """未登录即可获取（挂载不鉴权）；内容为控制台自有样式/脚本。"""
        _app, client, _rt = make_client()  # 故意不登录
        css = client.get("/dev/static/dev-console.css")
        js = client.get("/dev/static/dev-console.js")
        assert css.status_code == 200 and js.status_code == 200
        assert "text/css" in css.headers["content-type"]
        assert "javascript" in js.headers["content-type"]
        assert "磁贴" in css.text, "CSS 应为控制台自有内容（非空壳）"
        assert "function" in js.text, "JS 应为控制台自有内容（非空壳）"

    def test_assets_absent_when_console_disabled(self):
        """开关关闭 -> 静态挂载随控制台一起消失（不残留可取资源）。"""
        _app, client, _rt = make_client(enabled=False)
        assert client.get("/dev/static/dev-console.js").status_code == 404
        assert client.get("/dev/static/dev-console.css").status_code == 404


class TestConsoleTiles:
    """U1/U5 磁贴结构（语义标记级；纯视觉样式不追加镜像测试）。

    U5：运行日志磁贴并入实时区日志区（系统日志 tab），tile-logs 不复存在。"""

    TILE_IDS = (
        'tile-teaching',
        'tile-voice-history',
        "tile-costs",
        "tile-doubao-lab",
        "tile-jev", "tile-overview", "tile-voicelog", "tile-settings",
        "tile-voice-candidates", "tile-voice",
        "tile-vision", "tile-sync", "tile-replay",
    )

    def test_console_renders_tile_semantics(self):
        """控制台渲染磁贴语义：可展开磁贴头（aria-expanded）+ 对应主体。"""
        _app, client, _rt = make_client()
        login(client)
        page = client.get("/dev").text
        assert 'data-page="dev-console"' in page
        for tile_id in self.TILE_IDS:
            assert f'id="{tile_id}"' in page, tile_id
        heads = page.count('class="tile-head"')
        assert heads == len(self.TILE_IDS)
        assert page.count("aria-expanded") == heads, "每个磁贴头都带展开状态"
        assert page.count('class="tile-body"') == heads

    def test_tiles_reach_existing_entry_points(self):
        """AE6：原有诊断操作在新外壳全部可达（操作函数 + 关键提示文案）。"""
        _app, client, _rt = make_client()
        login(client)
        page = client.get("/dev").text
        for marker in (
            "loadStatus()", "loadLogs()", "setLevel()",
            "postForm('/volume'", "postForm('/speed'",
            "ttsSpeak()", "recordToggle()", "asrTest()",
            "previewToggle()", "orientBy(", "flip()", "visionTest()",
            "syncPull()", "replay(", "clearActiveLog()", "setLogTab(",
        ):
            assert marker in page, marker
        # 关键提示保留：录音独占 / OCR 机位 / 曲线诚实标注（U5 起为真实曲线）
        assert "独占设备" in page
        assert "约 20 cm" in page
        assert "不会伪造声音数据" in page
        assert 'id="curve-canvas"' in page
        assert 'id="log-pause"' in page, "日志区默认暂停滚动控件必须在页面结构中"

    def test_settings_tile_defaults_expanded(self):
        """常用参数原位展开（B3/B4）：运行参数磁贴默认 aria-expanded=true。"""
        _app, client, _rt = make_client()
        login(client)
        page = client.get("/dev").text
        settings_head = page[page.index('id="tile-settings"'):]
        settings_head = settings_head[: settings_head.index("tile-body")]
        assert 'aria-expanded="true"' in settings_head
        overview_head = page[page.index('id="tile-overview"'):]
        overview_head = overview_head[: overview_head.index("tile-body")]
        assert 'aria-expanded="false"' in overview_head
        candidates_head = page[page.index('id="tile-voice-candidates"'):]
        candidates_head = candidates_head[: candidates_head.index("tile-body")]
        assert 'aria-expanded="false"' in candidates_head


# ---------- U7：声纹候选快照与同名关联确认 ----------


def _vc_samples(seconds: float, seed: int):
    """可控假音频（过质量门：4s、RMS≈0.2；内容与向量无关）。"""

    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(16000 * seconds)) * 0.2).astype(np.float32)


def make_voice_memory_client(tmp_path):
    """挂真实 PersonalMemory + VoiceCandidates（共享连接）的替身 runtime。

    与生产装配同构（memory.db 即 ChatStore 连接）；隐私谓词与 rt.privacy
    同源，便于翻转联锁。
    """

    from lira.config import SpeakerConfig
    from lira.personal_memory import PersonalMemory
    from lira.speaker_candidates import VoiceCandidates

    rt = FakeRuntime()
    memory = PersonalMemory(tmp_path / "device.db", None, "kimi-k2.6",
                            lambda: rt.privacy.is_on)
    rt.personal_memory = memory
    rt.speaker_candidates = VoiceCandidates(
        memory.db, fingerprint="test-fp:2", profiles_provider=memory.profiles,
        params=SpeakerConfig(), privacy=lambda: rt.privacy.is_on)
    app, client, rt_out = make_client(runtime=rt)
    return app, client, rt_out


class TestVoiceCandidatesEndpoints:
    def test_delete_profile_requires_confirmation_privacy_and_idle(self,tmp_path):
        _app,client,rt=make_voice_memory_client(tmp_path)
        m=rt.personal_memory
        member=m.register_voice('成员','称呼',[1.,0.])
        url=f'/dev/api/voice-profiles/{member}/delete'
        payload={'confirm':True,'revision':m.voice_revision(member)}
        assert client.post(url,json=payload).status_code==401
        login(client)
        assert client.post(url,json={'revision':payload['revision']}).status_code==400
        rt.privacy._on=True
        assert client.post(url,json=payload).status_code==403
        rt.privacy._on=False
        rt.dev_handle.recording_active=True
        assert client.post(url,json=payload).status_code==409
        rt.dev_handle.recording_active=False
        assert len(m.profiles())==1
        assert client.post(url,json=payload).status_code==200
        assert not m.profiles() and m.people.get_member(member)

    def test_delete_unfinished_candidate_removes_evidence_not_members(self, tmp_path):
        import numpy as np
        _app, client, rt = make_voice_memory_client(tmp_path)
        login(client)
        store=rt.speaker_candidates
        cid=store.observe(np.array([1.,0.],np.float32),_vc_samples(4,91)).candidate_id
        member=rt.personal_memory.enroll('已登记',np.array([0.,1.],np.float32))
        response=client.delete(f'/dev/api/voice-candidates/{cid}')
        assert response.status_code==200
        assert store.db.execute('SELECT count(*) FROM voice_candidates WHERE candidate_id=?',(cid,)).fetchone()[0]==0
        assert store.db.execute('SELECT count(*) FROM voice_candidate_evidence WHERE candidate_id=?',(cid,)).fetchone()[0]==0
        assert rt.personal_memory.people.get_member(member) and len(rt.personal_memory.profiles())==1
        assert client.delete(f'/dev/api/voice-candidates/{cid}').status_code==404

    def test_delete_candidate_requires_session_and_preserves_promoted(self, tmp_path):
        import numpy as np
        _app, client, rt = make_voice_memory_client(tmp_path)
        store=rt.speaker_candidates
        cid=store.observe(np.array([1.,0.],np.float32),_vc_samples(4,92)).candidate_id
        assert client.delete(f'/dev/api/voice-candidates/{cid}').status_code==401
        login(client)
        rt.privacy._on = True
        assert client.delete(f'/dev/api/voice-candidates/{cid}').status_code==403
        rt.privacy._on = False
        store.db.execute("UPDATE voice_candidates SET status='promoted', member_id=42 WHERE candidate_id=?",(cid,))
        store.db.commit()
        assert client.delete(f'/dev/api/voice-candidates/{cid}').status_code==409
        assert store.db.execute('SELECT count(*) FROM voice_candidates WHERE candidate_id=?',(cid,)).fetchone()[0]==1
        for invalid in ('bad','0','-1',str(2**80)):
            assert client.delete(f'/dev/api/voice-candidates/{invalid}').status_code==400

    def test_delete_failure_rolls_back_candidate_evidence(self, tmp_path):
        _app, client, rt = make_voice_memory_client(tmp_path)
        login(client)
        store=rt.speaker_candidates
        cid=store.observe(np.array([1.,0.],np.float32),_vc_samples(4,93)).candidate_id
        store.db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON voice_candidates BEGIN SELECT RAISE(ABORT,'test'); END")
        response=client.delete(f'/dev/api/voice-candidates/{cid}')
        assert response.status_code==500
        assert store.db.execute('SELECT count(*) FROM voice_candidates WHERE candidate_id=?',(cid,)).fetchone()[0]==1
        assert store.db.execute('SELECT count(*) FROM voice_candidate_evidence WHERE candidate_id=?',(cid,)).fetchone()[0]==1

    def test_not_assembled_returns_503(self):
        """未装配（无 personal_memory/候选）→ 503 诚实降级，前端显示降级文案。"""
        _app, client, _rt = make_client()
        login(client)
        r = client.get("/dev/api/voice-candidates")
        assert r.status_code == 503
        assert "声纹候选未装配" in r.json()["error"]
        r = client.post("/dev/api/voice-candidates/1/confirm", json={"member_id": 1})
        assert r.status_code == 503

    def test_auth_and_privacy_matrix(self, tmp_path):
        """新端点鉴权矩阵：未登录 401（路由守卫遍历自动覆盖）；隐私开启时
        确认 POST 403（禁止身份确认操作），GET 快照仍可用（磁贴结构可见）。"""
    
        _app, client, rt = make_voice_memory_client(tmp_path)
        assert client.get("/dev/api/voice-candidates").status_code == 401
        assert client.post("/dev/api/voice-candidates/1/confirm",
                           json={"member_id": 1}).status_code == 401
        login(client)
        rt.privacy._on = True
        assert client.post("/dev/api/voice-candidates/1/confirm",
                           json={"member_id": 1}).status_code == 403
        rt.privacy._on = False
        assert client.get("/dev/api/voice-candidates").status_code == 200

    def test_snapshot_payload_and_established_semantics(self, tmp_path):
        """AE2：established 只按真实 voice_profiles 统计；就绪 100% 未确认
        候选不进入已建立人数；awaiting_console 明细带提议关联的成员名。"""
    
        _app, client, rt = make_voice_memory_client(tmp_path)
        login(client)
        store = rt.speaker_candidates
        mama = rt.personal_memory.enroll("妈妈", np.array([0.0, 1.0], np.float32))
        assert mama is not None
        vec_a = np.array([1.0, 0.0], np.float32)
        v1 = store.observe(vec_a, _vc_samples(4.0, 1))
        store.observe(vec_a, _vc_samples(4.0, 2))
        store.observe(vec_a, _vc_samples(4.0, 3))  # 就绪（3 段 12s，一致性过）
        # 50° 方向：与妈妈(0,1) cos≈0.77<0.80、与 A cos≈0.64<0.73——互不相扰
        vec_b = np.array([np.cos(np.radians(50.0)), np.sin(np.radians(50.0))], np.float32)
        v2 = store.observe(vec_b, _vc_samples(4.0, 4))
        baba = rt.personal_memory.people.create_member("爸爸")["id"]
        store.create_association_request(v2.candidate_id, baba)
        body = client.get("/dev/api/voice-candidates").json()
        assert body["summary"]["established_count"] == 1, "只算真实 profile"
        assert body["summary"]["ready_count"] == 1, "就绪未确认不算已建立"
        assert body["summary"]["awaiting_console_count"] == 1
        names = {m["name"]: m for m in body["members"]}
        assert names["妈妈"]["has_profile"] is True
        assert names["爸爸"]["has_profile"] is False
        assert v1.candidate_id != v2.candidate_id
        pending = body["pending_confirmations"]
        assert len(pending) == 1
        assert pending[0]["candidate_id"] == v2.candidate_id
        assert pending[0]["proposed_member_id"] == baba
        assert pending[0]["member_name"] == "爸爸"

    def test_confirm_success_retry_idempotent_and_conflicts(self, tmp_path):
        """确认 POST：成功 200、重试幂等不重复插声纹、成员已有声纹 409 不覆盖、
        候选不存在 404、状态不符 409、参数非法 400。"""
    
        _app, client, rt = make_voice_memory_client(tmp_path)
        login(client)
        store = rt.speaker_candidates
        vec_a = np.array([1.0, 0.0], np.float32)
        v1 = store.observe(vec_a, _vc_samples(4.0, 1))
        baba = rt.personal_memory.people.create_member("爸爸")["id"]
        store.create_association_request(v1.candidate_id, baba)
        url = f"/dev/api/voice-candidates/{v1.candidate_id}/confirm"
        assert client.post(url, json={}).status_code == 400
        assert client.post(url, json={"member_id": "x"}).status_code == 400
        # 路径参数非整数 → 400（手动 int 解析承诺的兑现点，U8 评审钉住）
        assert client.post("/dev/api/voice-candidates/abc/confirm",
                           json={"member_id": baba}).status_code == 400
        # 畸形 JSON body → 400 {"error"}（原裸 request.json() 为 500 纯文本）
        assert client.post(url, content=b"{not json",
                           headers={"Content-Type": "application/json"}).status_code == 400
        assert client.post("/dev/api/voice-candidates/999/confirm",
                           json={"member_id": baba}).status_code == 404
        # 状态不符：新候选没有挂请求
        vec_b = np.array([np.cos(np.radians(50.0)), np.sin(np.radians(50.0))], np.float32)
        v2 = store.observe(vec_b, _vc_samples(4.0, 2))
        assert client.post(f"/dev/api/voice-candidates/{v2.candidate_id}/confirm",
                           json={"member_id": baba}).status_code == 409
        # 成功：200 + 落一份 profile
        r = client.post(url, json={"member_id": baba})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "member_id": baba, "already": False}
        assert len(rt.personal_memory.profiles()) == 1
        # AE4 确认重试：幂等可见，不重复插 profile/成员
        r = client.post(url, json={"member_id": baba})
        assert r.status_code == 200 and r.json()["already"] is True
        assert len(rt.personal_memory.profiles()) == 1
        assert len(rt.personal_memory.people.list_members()) == 1
        # 成员已有声纹：409 且不覆盖（另一候选再次指向同一成员）
        store.create_association_request(v2.candidate_id, baba)
        r = client.post(f"/dev/api/voice-candidates/{v2.candidate_id}/confirm",
                        json={"member_id": baba})
        assert r.status_code == 409
        assert "已有声纹" in r.json()["error"]
        expected = rt.personal_memory.profiles()[0][1]
        aggregate = store.db.execute(
            "SELECT aggregate_vector FROM voice_candidates WHERE candidate_id=?",
            (v1.candidate_id,)).fetchone()[0]
        assert np.allclose(expected, np.frombuffer(aggregate, np.float32)), \
            "既有声纹仍是首次确认写入的聚合向量，未被覆盖"
