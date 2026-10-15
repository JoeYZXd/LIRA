"""开发者控制台路由（M8 计划 U1）：远程状态监控 / 配置 / 独立测试。

鉴权模型（计划 R9）：
  - 设备口令一次验证 → itsdangerous 签名 HttpOnly+SameSite=Strict 会话
    cookie（TTL 4h）；签名密钥首启随机生成、持久化于 store meta，
    **改口令即轮换**（旧会话全失效）。
  - 验证失败全局内存限速（5 次锁 5 分钟；单口令现实下全局计数即可，
    LAN 锁定循环骚扰为接受风险）；口令未设 fail-closed（引导设置）。
  - **响应形态**：整页 GET 未鉴权 → 重定向口令流程；dev JSON 数据端点
    未鉴权（缺失/过期/伪造）→ 一律 401 JSON（fetch 按钮不吞 HTML）。
  - 文本参数一律 POST body（禁 query string——不入访问日志/浏览器历史）。

并发纪律：变更类测试端点经 `dev_mutex` 串行（双击/双标签页 → 409）。
日志纪律：本模块只记路由级事件（操作 + 结果），文本/识别内容不落日志。

页面（dev-console 改版 U1，KTD1）：登录页与控制台页渲染自本包
templates/dev_login.html、templates/dev.html；CSS/JS 为本地静态资源
（static/dev-console.*），无构建链、无 CDN。模板与静态目录自管实例、
不从 ui.app 导入（避免 ui.app <-> ui.dev 循环导入）。

观察快照（dev-console 改版 U4，KTD4）：/dev/api/observe 聚合音频包络环、
文本事件环与状态面，游标增量 + reset 恢复，单次响应限事件条数；
/dev/api/settings 提供设置能力元信息（真实当前值/可写性/生效方式/约束），
写路径全部沿用既有端点（唯一例外是下方 U7 声纹确认的领域操作 POST——
不存在通用参数写入面）。

声纹候选（dev-console 改版 U7，KTD6）：/dev/api/voice-candidates 出候选/
已建立成员快照；同名关联确认是唯一的领域操作 POST（会话 + 隐私双闸，
409 拒绝覆盖既有声纹），仍无任何通用参数写入面。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lira.appliances.store import ApplianceStore
from lira.privacy import PassphraseVault
from lira.settings import DeviceSettings
from lira.speaker_candidates import CandidateConflict, CandidateNotFound

if TYPE_CHECKING:
    from lira.dev_telemetry import AudioFrame, TelemetryEvent

__all__ = [
    "DevHandle",
    "STATIC_DIR",
    "mount_dev",
    "reset_dev_session_secret",
    "SESSION_COOKIE",
]

logger = logging.getLogger("lira.dev")

_UI_DIR = Path(__file__).resolve().parent
#: dev 专用 Jinja 环境（与 ui/app.py 的 TEMPLATES_DIR 同一目录，实例各自持有）
templates = Jinja2Templates(directory=str(_UI_DIR / "templates"))
#: 控制台静态资源（CSS/JS）；仅 dev 挂载时存在，控制台关闭时不挂载
STATIC_DIR = _UI_DIR / "static"

SESSION_COOKIE = "lira_dev_session"
_SESSION_TTL_SECONDS = 4 * 3600
_SECRET_META_KEY = "dev_console_session_secret"
_SALT = "lira-dev-session"

_MAX_FAILED = 5
_LOCK_SECONDS = 300.0
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: 设置约束字面量单一出处（U8 评审：settings 元信息与写端点校验曾各抄一份，
#: 单边改动即契约漂移）。写端点校验与 _settings_items 皆引用此处。
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_CAMERA_ROTATIONS = (0, 90, 180, 270)
#: observe 单次响应的事件条数上限（剩余经 next_cursor 下次续读；
#: 250ms 轮询节奏下每次 100 条足够追平增量，KTD4）
_OBSERVE_EVENT_PAGE = 100

#: 录音内存缓冲（零落盘；进程内，覆盖复用）。值语义：仅 "wav" 键承载
#: bytes | None（"ts" 存 float 时间戳——U8 评审：原注解与实际不符）
clip_state: dict[str, bytes | float | None] = {"wav": None, "ts": 0.0}


class DevHandle:
    """dev 控制台对运行时能力的访问面（弱耦合：持 runtime 引用按需取）。"""

    def __init__(self, runtime) -> None:  # DeviceRuntime（鸭子类型避免循环导入）
        self.runtime = runtime
        #: 变更类测试端点的互斥锁（ASR/视觉/回放；进行中 → 409）
        self.dev_mutex: asyncio.Lock = asyncio.Lock()
        #: 录音会话状态（start/stop 流；独占麦克风窗口由 runtime.mic_pause 承担）
        self.recording_active = False
        self._rec_task: asyncio.Task | None = None
        self._rec_stop: asyncio.Event | None = None
        self._rec_result: tuple[bytes, float] | None = None

    @property
    def store(self) -> ApplianceStore:
        return self.runtime.store

    @property
    def vault(self) -> PassphraseVault:
        return self.runtime.vault

    async def start_recording(self, max_seconds: float = 60.0) -> None:
        """开始录音：暂停主循环采集独占设备 -> 后台采集循环（手动/上限停止）。"""
        if self.recording_active:
            raise RuntimeError("录音已在进行中")
        self.recording_active = True
        self._rec_stop = asyncio.Event()
        self._rec_result = None
        try:
            await self.runtime.mic_pause()  # 独占设备（板上实测双流并行被饿死）
        except Exception:
            self.recording_active = False
            raise
        self._rec_task = asyncio.ensure_future(self._record_loop(max_seconds))

    async def stop_recording(self) -> tuple[bytes, float]:
        """结束录音：返回 (wav 字节, 实际秒数)；未在录音返回 (b"", 0.0)。"""
        if not self.recording_active:
            return b"", 0.0
        if self._rec_stop is not None:
            self._rec_stop.set()
        task = self._rec_task
        if task is not None:
            try:
                await task
            except Exception:  # noqa: BLE001 - 采集循环异常按空结果处理
                self._rec_result = (b"", 0.0)
        self.recording_active = False
        await self.runtime.mic_resume()
        wav, seconds = self._rec_result or (b"", 0.0)
        self._rec_task = None
        return wav, seconds

    async def _record_loop(self, max_seconds: float) -> None:
        """采集循环：静音窗口内独占录音，直至 stop 事件或时长上限。"""
        import io
        import wave

        import sounddevice as sd

        from lira.audio._paths import SAMPLE_RATE

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=1600,
            callback=lambda indata, frames, t, status: loop.call_soon_threadsafe(
                queue.put_nowait, bytes(indata)
            ),
        )
        stream.start()
        chunks: list[bytes] = []
        started = loop.time()
        try:
            while loop.time() - started < max_seconds and not self._rec_stop.is_set():
                try:
                    chunks.append(await asyncio.wait_for(queue.get(), timeout=0.5))
                except TimeoutError:
                    continue
        finally:
            stream.stop()
            stream.close()
        if not self._rec_stop.is_set():
            # 自然到期自愈（U8 对抗评审 F5，pre-existing）：60s 上限到达时
            # 复位录音态并归还麦克风——原实现只停采集，recording_active 悬挂
            # （再 start 409）且主循环 mic_pause 不解除（设备失聪到手动 stop）。
            # 手动 stop 路径不受影响（stop 事件已置位时由 stop_recording 收尾）。
            self.recording_active = False
            self._rec_result = (b"".join(chunks), loop.time() - started)
            await self.runtime.mic_resume()
        seconds = loop.time() - started
        pcm = b"".join(chunks)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        self._rec_result = (buf.getvalue(), seconds)


def _session_secret(store: ApplianceStore) -> str:
    """取（或首启生成并持久化）会话签名密钥。"""
    secret = store.get_meta(_SECRET_META_KEY)
    if not secret:
        secret = secrets.token_urlsafe(48)
        store.set_meta(_SECRET_META_KEY, secret)
        logger.info("dev event=session_secret_created")
    return secret


def reset_dev_session_secret(store: ApplianceStore) -> None:
    """轮换签名密钥（改口令时调用）：既有会话 cookie 全部失效。"""
    store.set_meta(_SECRET_META_KEY, secrets.token_urlsafe(48))
    logger.info("dev event=session_secret_rotated")


def _serializer(store: ApplianceStore):
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(_session_secret(store), salt=_SALT)


@dataclasses.dataclass
class _RateLimiter:
    """口令验证失败全局计数（内存；重启清零）。"""

    failures: int = 0
    locked_until: float = 0.0

    def check(self) -> str | None:
        """返回锁定剩余秒数（未锁返回 None）。"""
        remaining = self.locked_until - time.monotonic()
        return max(0.0, remaining) if remaining > 0 else None

    def record_failure(self) -> float | None:
        self.failures += 1
        if self.failures >= _MAX_FAILED:
            self.locked_until = time.monotonic() + _LOCK_SECONDS
            self.failures = 0
            return _LOCK_SECONDS
        return None

    def reset(self) -> None:
        self.failures = 0
        self.locked_until = 0.0


def _decode_jpeg(raw: bytes):
    """JPEG 字节 → BGR 图像（CPU 密集，调用方 to_thread）。"""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("拍摄内容无法解码")
    return image


def _downscale_jpeg_b64(raw: bytes, width: int = 640) -> str:
    """JPEG → 缩放宽 640 的 JPEG base64（预览/回显；CPU 密集，调用方 to_thread）。"""
    import base64

    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("图像解码失败")
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)))
    ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ok:
        raise ValueError("图像编码失败")
    return base64.b64encode(jpeg.tobytes()).decode("ascii")


def _offline_transcribe(asr, wav_bytes: bytes) -> str:
    """wav 字节 -> 离线喂独立 AsrStream -> 识别文本（CPU 密集，调用方 to_thread）。"""
    import io
    import wave

    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    stream = asr.create_stream()
    stream.feed(samples)
    return stream.text().strip()


def _classify_text(runtime, text: str) -> dict | None:
    """本地规则意图分类展示（不执行；与 Router 同源的 intents 规则）。"""
    from lira.dialog.intents import match_local

    appliances = tuple(a.to_dialog() for a in runtime.store.get_all_appliances())
    intent = match_local(text, appliances)
    if intent is None:
        return None
    result: dict = {"kind": intent.kind.value}
    if intent.action is not None:
        result["action"] = intent.action
    if intent.appliance is not None:
        result["appliance"] = intent.appliance.name
    return result


# ---------- 观察快照 / 设置能力（U4 接口层：序列化与元信息在此，不进遥测层） ----------


def _loglevel_name() -> str:
    """生效日志级别名（未显式设置时沿继承链取有效级别，恒有真实名字）。"""
    return logging.getLevelName(logging.getLogger("lira").getEffectiveLevel())


def _camera_oriented(runtime) -> object | None:
    """支持方向设置的相机（缺失/不支持返回 None——status 与 settings 同源判断）。"""
    camera = getattr(runtime, "camera", None)
    return camera if camera is not None and hasattr(camera, "rotation") else None


def _status_payload(handle: DevHandle) -> dict:
    """/dev/api/status 与 /dev/api/observe 共用的状态载荷（两处同构）。

    U1 真实设置回读语义不变：值一律取运行时真实状态；允许范围与
    DeviceSettings 服务端校验同源（类常量），前端据此做客户端预检。
    """
    rt = handle.runtime
    s = rt.status()
    s["sync_detail"] = dict(rt.sync_status)
    s["volume"] = rt.settings.volume
    s["tts_speed"] = rt.settings.tts_speed
    s["volume_range"] = list(DeviceSettings.VOLUME_RANGE)
    s["speed_range"] = list(DeviceSettings.SPEED_RANGE)
    s["loglevel"] = _loglevel_name()
    # 相机实际方向；相机缺失（未装配/不支持）时省略字段
    camera = _camera_oriented(rt)
    if camera is not None:
        s["camera_rotation"] = camera.rotation
        s["camera_hflip"] = bool(getattr(camera, "hflip", False))
    return s


def _audio_frame_json(frame: "AudioFrame") -> dict:
    """AudioFrame → JSON dict。字段与 dataclass 一一对应，用 asdict 免手抄
    （U8 化简审查：手写映射在加字段时会静默丢字段）。"""
    return dataclasses.asdict(frame)


def _event_json(event: "TelemetryEvent") -> dict:
    """TelemetryEvent → JSON dict（utterance/turn/owner 缺省保留 null）。

    text_bytes 是环淘汰的内部簿记（U8 评审：asdict 会把内部字段自动变成
    API 面）——显式摘除，公开载荷只含计划声明的事件 schema。
    """
    payload = dataclasses.asdict(event)
    payload.pop("text_bytes", None)
    return payload


def _parse_cursor(raw: str | None) -> int | None:
    """observe 的 query 游标解析：缺失/非法（非整数、负数）→ None。

    None = 按首次拉取语义处理（整窗 + reset，客户端据此重建基线），
    可恢复且绝不 500。query 只携带游标序号与实例 id（数值/十六进制串，
    非口令/内容类文本），不违反"文本参数 POST body"纪律。
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


async def _json_body(request: Request) -> dict | None:
    """POST JSON body 解析（U8 评审收口，原六处裸 await request.json()）：
    非法 JSON / 非 dict（标量、数组）→ None，调用方统一回 400——绝不 500
    纯文本（鉴权后的自纠错契约与端点其余 400/404/409 形态一致）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - JSONDecodeError/空 body/编码错误同路
        return None
    return body if isinstance(body, dict) else None


def _writable_item(key: str, label: str, value, constraint: dict,
                   write_endpoint: str) -> dict:
    """可写设置项样板（U8 化简审查：writable/effective 两常量键不再逐项手抄）。"""
    return {"key": key, "label": label, "value": value, "writable": True,
            "effective": "immediate", "constraint": constraint,
            "write_endpoint": write_endpoint}


def _readonly_item(key: str, label: str, value) -> dict:
    """只读项：仅能力元信息，constraint/write_endpoint 恒空。"""
    return {"key": key, "label": label, "value": value, "writable": False,
            "effective": "immediate", "constraint": None, "write_endpoint": None}


def _settings_items(runtime) -> list[dict]:
    """设置能力元信息（U4/R11）：真实当前值 + 可写性 + 生效方式 + 取值约束。

    只描述既有写路径（write_endpoint 指向既有 POST 端点），不新增写面；
    取值与 /dev/api/status 同源（_loglevel_name/_camera_oriented——U8 化简
    审查：原两处表达式重复，靠注释维持"同源互证"）；只读项值取自 status。
    """
    settings = runtime.settings
    status = runtime.status()
    items: list[dict] = [
        _writable_item(
            "volume", "音量", settings.volume,
            {"type": "range", "min": DeviceSettings.VOLUME_RANGE[0],
             "max": DeviceSettings.VOLUME_RANGE[1]}, "/volume"),
        _writable_item(
            "tts_speed", "语速", settings.tts_speed,
            {"type": "range", "min": DeviceSettings.SPEED_RANGE[0],
             "max": DeviceSettings.SPEED_RANGE[1]}, "/speed"),
        _writable_item(
            "loglevel", "日志级别", _loglevel_name(),
            {"type": "choices", "values": list(_LOG_LEVELS)},
            "/dev/api/loglevel"),
    ]
    # 相机项随装配出现（与 /status 相机回读同一存在性判断）
    camera = _camera_oriented(runtime)
    if camera is not None:
        items.append(_writable_item(
            "camera_rotation", "相机方向", camera.rotation,
            {"type": "choices", "values": list(_CAMERA_ROTATIONS)},
            "/dev/api/camera/orientation"))
        items.append(_writable_item(
            "camera_hflip", "相机水平翻转", bool(getattr(camera, "hflip", False)),
            {"type": "bool"}, "/dev/api/camera/orientation"))
    # 只读项：仅能力元信息
    items.append(_readonly_item("models", "模型就绪状态", status.get("models", {})))
    items.append(_readonly_item("sync_session", "同步会话",
                                (status.get("sync") or {}).get("session")))
    return items


def mount_dev(app: FastAPI, handle: DevHandle) -> None:
    """向设备 UI 应用挂载 dev 路由（仅 dev_console.enabled 时由 create_app 调用）。"""
    limiter = _RateLimiter()

    # 静态资源（CSS/JS）：与 dev 路由同生命周期——仅控制台开启时挂载。
    # 无鉴权可匿名获取：CSS/JS 不含敏感内容（tests/test_dev_console.py
    # 的 TestStaticAssets 固定该事实，将来若放敏感内容须改造为鉴权挂载）。
    app.mount("/dev/static", StaticFiles(directory=str(STATIC_DIR)), name="dev-static")

    def _issue_cookie(response: JSONResponse) -> JSONResponse:
        token = _serializer(handle.store).dumps("dev-session")
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
        )
        return response

    def _session_ok(request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        try:
            _serializer(handle.store).loads(token, max_age=_SESSION_TTL_SECONDS)
            return True
        except Exception:  # noqa: BLE001 - 签名/过期一律按未鉴权
            return False

    def _require_session(request: Request, *, api: bool):
        """页面 → 重定向口令流程；数据端点 → 401 JSON。返回响应或 None（已鉴权）。"""
        if _session_ok(request):
            return None
        if api:
            return JSONResponse({"error": "未鉴权（会话缺失/过期）"}, status_code=401)
        return RedirectResponse("/passphrase/setup", status_code=303)

    async def _privacy_reject() -> JSONResponse | None:
        """隐私联锁（采集类测试的权威闸，服务端逐端点检查）。"""
        if handle.runtime.privacy.is_on:
            return JSONResponse(
                {"error": "隐私模式开启，采集类测试不可用（请先关闭隐私）。"},
                status_code=403,
            )
        return None

    # ---------- 页面与登录 ----------

    @app.get("/dev")
    async def dev_page(request: Request):
        if _session_ok(request):
            return templates.TemplateResponse(request, "dev.html", {})
        if not handle.vault.is_set():
            # 首启引导：口令未设（fail-closed，去设备 UI 的设置页）
            return RedirectResponse("/passphrase/setup", status_code=303)
        # 口令已设：返回控制台专属登录页（浏览器唯一登录入口）
        return templates.TemplateResponse(request, "dev_login.html", {})

    @app.post("/dev/login")
    async def dev_login(request: Request):
        if not handle.vault.is_set():
            return JSONResponse(
                {"error": "设备口令未设置（先完成首启设置）。"}, status_code=409
            )
        locked = limiter.check()
        if locked is not None:
            return JSONResponse(
                {"error": f"失败次数过多，请 {int(locked)} 秒后再试。"}, status_code=429
            )
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body 须为合法 JSON 对象"}, status_code=400)
        if handle.vault.verify(str(body.get("passphrase", ""))):
            limiter.reset()
            logger.info("dev event=login ok=true")
            return _issue_cookie(JSONResponse({"ok": True}))
        locked_after = limiter.record_failure()
        logger.info("dev event=login ok=false")
        if locked_after is not None:
            return JSONResponse(
                {"error": f"失败次数过多，已锁定 {int(locked_after)} 秒。"}, status_code=429
            )
        return JSONResponse({"error": "口令不正确。"}, status_code=401)

    # ---------- 状态 / 日志 / 运行参数（U2） ----------

    @app.get("/dev/api/status")
    async def dev_status(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        return JSONResponse(_status_payload(handle))

    @app.get("/dev/api/logs")
    async def dev_logs(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        from lira.main import RING_LOG_HANDLER

        return JSONResponse({"lines": RING_LOG_HANDLER.snapshot()[-100:]})

    @app.get("/dev/api/voice-log")
    async def dev_voice_log(request: Request):
        """语音交互日志（内存环形区；条目可含识别/回复文本——展示通道特例）。"""
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        from lira.voice_log import VOICE_LOG

        return JSONResponse({
            "entries": [e.to_json() for e in VOICE_LOG.snapshot()],
            "state": handle.runtime.engine.state.value,
        })

    @app.post("/dev/api/voice-log/clear")
    async def dev_voice_log_clear(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        from lira.voice_log import VOICE_LOG

        VOICE_LOG.clear()
        logger.info("dev event=voice_log_clear")
        return JSONResponse({"ok": True})

    @app.post("/dev/api/loglevel")
    async def dev_loglevel(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body 须为合法 JSON 对象"}, status_code=400)
        level = str(body.get("level", "")).upper()
        if level not in _LOG_LEVELS:
            return JSONResponse({"error": "级别须为 DEBUG/INFO/WARNING/ERROR"}, status_code=400)
        logging.getLogger("lira").setLevel(getattr(logging, level))
        logger.info("dev event=loglevel level=%s", level)
        return JSONResponse({"ok": True, "level": level})

    # ---------- 观察快照与设置能力（dev-console 改版 U4，KTD4） ----------

    @app.get("/dev/api/observe")
    async def dev_observe(request: Request):
        """聚合观察快照：音频包络环 + 文本事件环 + 状态面，一次往返。

        边界次序固定：先会话（401）后隐私（403）——隐私开启时返回 403 且
        不含任何 items（接口面第二道闸；遥测联锁此刻已清空两环，这里保证
        缓冲不经旁路泄漏）。游标/reset 语义完全复用
        DevTelemetry.audio_since/events_since（实例不匹配、游标越界、请求
        区间已被淘汰或清空 → reset=True 并附整窗），接口层不重造判断。
        """
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        privacy_denied = await _privacy_reject()
        if privacy_denied is not None:
            return privacy_denied
        telemetry = getattr(handle.runtime, "telemetry", None)
        if telemetry is None:
            return JSONResponse({"error": "遥测未装配"}, status_code=503)
        # 页面轮询即观察（U5 接线，KTD2）：每次拉取续期 2s 活动窗——页面停拉
        # （折叠/隐藏/断开）后包络计算自然停止，无需显式生命周期回调
        telemetry.note_observer_activity()
        params = request.query_params
        audio_cursor = _parse_cursor(params.get("audio_cursor"))
        event_cursor = _parse_cursor(params.get("event_cursor"))
        instance = params.get("instance") or None

        # 缺省游标 = 首次拉取：整窗 + reset=True（客户端据此整体建基线）
        audio_items, audio_next, audio_reset = telemetry.audio_since(
            0 if audio_cursor is None else audio_cursor, instance_id=instance)
        if audio_cursor is None:
            audio_reset = True
        # 音频环上限即 100 帧（≈10 秒），整窗本就一次能取完，不额外截断
        event_items, event_next, event_reset = telemetry.events_since(
            0 if event_cursor is None else event_cursor, instance_id=instance)
        if event_cursor is None:
            event_reset = True
        if len(event_items) > _OBSERVE_EVENT_PAGE:
            # 单响应限流：next_cursor 只推进到本次最后一条，剩余续读
            # （len(items)==上限时可能还有后续；reset 分页重建时同样以此为据）
            event_items = event_items[:_OBSERVE_EVENT_PAGE]
            event_next = event_items[-1].seq
        # 日志纪律：只记数量与布尔，不记事件/音频内容。U8 效率审查：250ms
        # 轮询下 INFO 会以 4 行/秒冲刷环形日志（控制台日志视图自己就是受害
        # 者）——常规拍降 debug，仅 reset（实例重启/游标失效）留 INFO 痕。
        if audio_reset or event_reset:
            logger.info("dev event=observe reset=true n_audio=%d n_events=%d",
                        len(audio_items), len(event_items))
        else:
            logger.debug("dev event=observe n_audio=%d n_events=%d",
                         len(audio_items), len(event_items))
        return JSONResponse({
            "instance_id": telemetry.instance_id,
            "audio": {
                "items": [_audio_frame_json(f) for f in audio_items],
                "next_cursor": audio_next,
                "reset": audio_reset,
            },
            "events": {
                "items": [_event_json(e) for e in event_items],
                "next_cursor": event_next,
                "reset": event_reset,
                # 页长回显：客户端续页判断以此为据，不再各自硬编码（U8 化简审查）
                "page": _OBSERVE_EVENT_PAGE,
            },
            "status": _status_payload(handle),
        })

    @app.get("/dev/api/settings")
    async def dev_settings(request: Request):
        """设置能力元信息：真实当前值 + 可写性 + 生效方式 + 取值约束。

        只描述能力，不承载写入——音量/语速/级别/相机的写入仍走既有 POST
        端点（write_endpoint 即指向它们），不存在通用参数写接口，页面不能
        经本接口绕过领域操作（R11/KTD6）。
        """
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        return JSONResponse({"settings": _settings_items(handle.runtime)})

    # ---------- 声纹候选与成员（U7，KTD6） ----------

    @app.get("/dev/api/voice-candidates")
    async def dev_voice_candidates(request: Request):
        """候选/已建立成员快照（磁贴数据面；低频单拉，不进 observe 节拍）。

        established_count 来自真实 voice_profiles（AE2：采集 100% 但未确认
        的候选不计已建立人数）；awaiting_console 明细带提议关联的成员名，
        供确认卡片展示。日志只记事件，不记成员名/称呼内容。
        """
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        rt = handle.runtime
        if (getattr(rt, "speaker_candidates", None) is None
                or getattr(rt, "personal_memory", None) is None):
            return JSONResponse({"error": "声纹候选未装配"}, status_code=503)
        profiles = rt.personal_memory.profiles()
        snap = rt.speaker_candidates.snapshot(established=profiles)
        profiles_ids = {member for member, _vector in profiles}
        members = [
            {"member_id": m["id"], "name": m["name"], "emoji": m["emoji"],
             "has_profile": m["id"] in profiles_ids}
            for m in rt.personal_memory.people.list_members()
        ]
        by_id = {m["member_id"]: m for m in members}
        pending = []
        for c in snap["candidates"]:
            if c["status"] == "awaiting_console" and c.get("proposed_member_id"):
                member = by_id.get(c["proposed_member_id"])
                pending.append({
                    "candidate_id": c["candidate_id"],
                    "proposed_member_id": c["proposed_member_id"],
                    "member_name": member["name"] if member else None,
                    "progress": c["progress"],
                    "segment_count": c["segment_count"],
                    "total_seconds": c["total_seconds"],
                })
        logger.info("dev event=voice_candidates n=%d awaiting=%d",
                    len(snap["candidates"]), len(pending))
        return JSONResponse({**snap, "members": members,
                             "pending_confirmations": pending})

    @app.delete("/dev/api/voice-candidates/{candidate_id}")
    async def dev_voice_candidate_delete(candidate_id: str, request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        denied = await _privacy_reject()
        if denied is not None:
            return denied
        candidates = getattr(handle.runtime, "speaker_candidates", None)
        if candidates is None:
            return JSONResponse({"error": "声纹候选未装配"}, status_code=503)
        try:
            candidate_id_int = int(candidate_id)
            if not 0 < candidate_id_int < 2**63:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "candidate_id 须为整数"}, status_code=400)
        try:
            candidates.delete_unfinished(candidate_id_int)
        except CandidateNotFound as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except CandidateConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except sqlite3.Error:
            logger.warning("dev event=voice_candidate_delete_failed")
            return JSONResponse({"error": "删除失败，请稍后重试"}, status_code=500)
        identity = getattr(handle.runtime, "speaker_identity", None)
        pending = getattr(identity, "pending", None)
        if pending and pending.get('candidate_id') == candidate_id_int:
            identity.pending = None
        return JSONResponse({"deleted": True, "candidate_id": candidate_id_int})

    @app.post("/dev/api/voice-candidates/{candidate_id}/confirm")
    async def dev_voice_candidate_confirm(candidate_id: str, request: Request):
        """确认同名关联（领域操作端点，无通用参数写面，KTD6）。

        边界次序：会话（401）→ 隐私（403——隐私开启禁止身份确认操作）→
        装配（503）→ 参数（400）→ 领域事务（404 候选/成员不存在、
        409 状态不符或成员已有声纹——绝不覆盖既有 profile）。
        路径参数手动解析为 int（不用 FastAPI 类型校验）：会话/隐私闸必须
        先于参数校验，未鉴权请求一律 401 而非 422。
        """
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        privacy_denied = await _privacy_reject()
        if privacy_denied is not None:
            return privacy_denied
        candidates = getattr(handle.runtime, "speaker_candidates", None)
        if candidates is None:
            return JSONResponse({"error": "声纹候选未装配"}, status_code=503)
        try:
            candidate_id_int = int(candidate_id)
        except ValueError:
            return JSONResponse({"error": "candidate_id 须为整数"}, status_code=400)
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body 须为合法 JSON 对象"}, status_code=400)
        # U8 对抗评审 F2：await body 读取是让出点——隐私可在首闸之后、领域
        # 事务之前翻转（confirm_association 无内部隐私检查），此处复检对齐
        # observe 的双闸先例
        privacy_denied = await _privacy_reject()
        if privacy_denied is not None:
            return privacy_denied
        member_id = body.get("member_id")
        if not isinstance(member_id, int) or isinstance(member_id, bool):
            return JSONResponse({"error": "member_id 须为整数"}, status_code=400)
        try:
            result = candidates.confirm_association(candidate_id_int, member_id)
        except CandidateNotFound as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except CandidateConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except sqlite3.Error as exc:
            # 意外存储异常（磁盘满等）：事务已由 with self.db 回滚，无数据
            # 风险——返回 JSON 500 而非裸文本 traceback（U8 评审）
            logger.warning("dev event=voice_candidate_confirm_failed kind=%s",
                           type(exc).__name__)
            return JSONResponse({"error": "确认事务失败，请稍后重试"}, status_code=500)
        # 日志纪律：只记 id 与幂等布尔，不记成员名/称呼
        logger.info("dev event=voice_candidate_confirm candidate=%d member=%d already=%s",
                    candidate_id_int, result["member_id"], result.get("already", False))
        return JSONResponse({"ok": True, "member_id": result["member_id"],
                             "already": result.get("already", False)})

    # ---------- 语音链路测试（U3） ----------

    @app.post("/dev/api/tts")
    async def dev_tts(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body 须为合法 JSON 对象"}, status_code=400)
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "缺少播报文本"}, status_code=400)
        if len(text) > 200:
            return JSONResponse({"error": "播报文本过长（<=200 字符）"}, status_code=400)
        tts = handle.runtime.tts
        if tts is None:
            return JSONResponse({"error": "语音栈未就绪"}, status_code=503)
        started = time.monotonic()
        done = tts.speak(text, volume=handle.runtime.settings.volume,
                         speed=handle.runtime.settings.tts_speed)
        try:
            await asyncio.wait_for(done.wait(), timeout=10.0)
            result = "spoken"
        except TimeoutError:
            result = "timeout"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # 日志纪律：只记事件与耗时，播报文本不落日志
        logger.info("dev event=tts_test result=%s elapsed_ms=%d", result, elapsed_ms)
        return JSONResponse({"result": result, "elapsed_ms": elapsed_ms})

    @app.post("/dev/api/record/start")
    async def dev_record_start(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        privacy_denied = await _privacy_reject()
        if privacy_denied is not None:
            return privacy_denied
        if handle.recording_active:
            return JSONResponse({"error": "录音已在进行中"}, status_code=409)
        try:
            await handle.start_recording(60.0)  # 硬上限 60s（防忘记停止）
        except Exception as exc:  # noqa: BLE001 - 设备错误折叠为友好响应
            logger.warning("dev event=record_start error=%s", type(exc).__name__)
            return JSONResponse({"error": f"录音启动失败: {exc}"}, status_code=503)
        logger.info("dev event=record_start max_seconds=60")
        return JSONResponse({"recording": True, "max_seconds": 60})

    @app.post("/dev/api/record/stop")
    async def dev_record_stop(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        if not handle.recording_active:
            return JSONResponse({"error": "当前没有进行中的录音"}, status_code=400)
        wav, seconds = await handle.stop_recording()
        if handle.runtime.privacy.is_on and wav:
            # 录音中途开启隐私：严格联锁 -> 丢弃已采内容
            clip_state["wav"] = None
            logger.info("dev event=record_stop discarded=privacy_on")
            return JSONResponse(
                {"discarded": True, "reason": "录音中途开启隐私，已按联锁丢弃"}
            )
        clip_state["wav"] = wav
        clip_state["ts"] = time.time()
        logger.info("dev event=record_stop seconds=%.1f bytes=%d", seconds, len(wav))
        return JSONResponse(
            {"ok": True, "seconds": round(seconds, 1), "bytes": len(wav)}
        )

    @app.get("/dev/api/clip")
    async def dev_clip(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        wav = clip_state.get("wav")
        if not wav:
            return JSONResponse({"error": "尚未录音（先点击录音）"}, status_code=404)
        from fastapi.responses import Response

        return Response(content=wav, media_type="audio/wav")

    @app.post("/dev/api/asr")
    async def dev_asr(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        privacy_denied = await _privacy_reject()
        if privacy_denied is not None:
            return privacy_denied
        if handle.recording_active:
            return JSONResponse({"error": "录音进行中（先结束录音再识别）"}, status_code=409)
        form = await request.form()
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            data = await upload.read()
            if len(data) > _MAX_UPLOAD_BYTES:
                return JSONResponse({"error": "上传文件过大（<=5MB）"}, status_code=413)
            wav_bytes = data
        else:
            wav_bytes = clip_state.get("wav")
        if not wav_bytes:
            return JSONResponse({"error": "尚无录音（先录音或上传 wav）"}, status_code=400)
        asr = getattr(handle.runtime, "asr", None)
        if asr is None:
            return JSONResponse({"error": "ASR 引擎未就绪"}, status_code=503)
        try:
            text = await asyncio.to_thread(_offline_transcribe, asr, wav_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dev event=asr error=%s", type(exc).__name__)
            return JSONResponse({"error": f"识别失败: {exc}"}, status_code=503)
        intent = _classify_text(handle.runtime, text)
        # 日志纪律：识别文本只在响应往返，不落日志
        logger.info("dev event=asr chars=%d matched=%s", len(text), intent is not None)
        return JSONResponse({"text": text, "intent": intent})

    # ---------- 视觉链路测试（U4） ----------

    @app.post("/dev/api/vision")
    async def dev_vision(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        privacy_denied = await _privacy_reject()
        if privacy_denied is not None:
            return privacy_denied
        if handle.dev_mutex.locked():
            return JSONResponse({"error": "测试进行中，请稍后再试"}, status_code=409)
        camera = getattr(handle.runtime, "camera", None)
        ocr = getattr(handle.runtime, "ocr", None)
        if camera is None or ocr is None:
            return JSONResponse({"error": "相机/OCR 未就绪"}, status_code=503)
        async with handle.dev_mutex:
            started = time.monotonic()
            try:
                raw = await camera.capture()
            except Exception as exc:  # noqa: BLE001 - HAL 错误族折叠为友好响应
                logger.warning("dev event=vision error=capture %s", type(exc).__name__)
                return JSONResponse({"error": f"拍摄失败: {exc}"}, status_code=503)
            t_capture = time.monotonic() - started
            image = await asyncio.to_thread(_decode_jpeg, raw)
            preview_b64 = await asyncio.to_thread(_downscale_jpeg_b64, raw, 1280)
            t_decode = time.monotonic() - started - t_capture
            polygons = await asyncio.to_thread(ocr.detect, image)
            t_det = time.monotonic() - started - t_capture - t_decode
            from lira.vision.engine import crop_rotated_box
            from lira.vision.layout import sort_reading_order

            ordered = sort_reading_order(polygons)
            texts: list[str] = []
            rec_started = time.monotonic()
            for poly in ordered:
                crop = crop_rotated_box(image, poly)
                texts.append(await asyncio.to_thread(ocr.recognize, crop))
            t_rec = time.monotonic() - rec_started
        timings = {
            "capture_ms": int(t_capture * 1000),
            "decode_ms": int(t_decode * 1000),
            "det_ms": int(t_det * 1000),
            "rec_ms": int(t_rec * 1000),
        }
        logger.info(
            "dev event=vision lines=%d det_ms=%d rec_ms=%d",
            len(ordered), timings["det_ms"], timings["rec_ms"],
        )
        return JSONResponse(
            {
                "text": "\n".join(t.strip() for t in texts if t.strip()),
                "timings_ms": timings,
                "image": "data:image/jpeg;base64," + preview_b64,
            }
        )

    # ---------- 视觉预览（实时画面 = 慢速单帧轮询；相机路径 ~0.5fps 级） ----------

    @app.get("/dev/api/vision/frame")
    async def dev_vision_frame(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        privacy_denied = await _privacy_reject()
        if privacy_denied is not None:
            return privacy_denied
        camera = getattr(handle.runtime, "camera", None)
        if camera is None:
            return JSONResponse({"error": "相机未就绪"}, status_code=503)
        try:
            raw = await camera.capture()
            jpeg = await asyncio.to_thread(_downscale_jpeg_b64, raw)
        except Exception as exc:  # noqa: BLE001 - HAL/解码错误族折叠
            logger.warning("dev event=vision_frame error=%s", type(exc).__name__)
            return JSONResponse({"error": f"取帧失败: {exc}"}, status_code=503)
        logger.info("dev event=vision_frame")
        import base64

        from fastapi.responses import Response

        return Response(
            content=base64.b64decode(jpeg),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    # ---------- 相机方向控制 ----------

    @app.post("/dev/api/camera/orientation")
    async def dev_camera_orientation(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        camera = getattr(handle.runtime, "camera", None)
        if camera is None or not hasattr(camera, "rotation"):
            return JSONResponse({"error": "相机不支持方向设置"}, status_code=501)
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body 须为合法 JSON 对象"}, status_code=400)
        rotation = body.get("rotation")
        rotate_by = body.get("rotate_by")
        hflip = body.get("hflip")
        if rotate_by is not None:
            # 相对旋转：每次点击在当前方向上累加（多次点击循环 0→90→180→270→0）
            if rotate_by not in (90, 180, 270, -90):
                return JSONResponse(
                    {"error": "rotate_by 须为 90/180/270/-90"}, status_code=400
                )
            rotation = (getattr(camera, "rotation", 0) + rotate_by) % 360
        if rotation is not None:
            if rotation not in _CAMERA_ROTATIONS:
                return JSONResponse({"error": "rotation 须为 0/90/180/270"}, status_code=400)
            camera.rotation = rotation
            handle.store.set_meta("camera_rotation", str(rotation))
        if hflip is not None:
            camera.hflip = bool(hflip)
            handle.store.set_meta("camera_hflip", "1" if camera.hflip else "0")
        logger.info("dev event=camera_orientation rotation=%s hflip=%s",
                    camera.rotation, camera.hflip)
        return JSONResponse({"rotation": camera.rotation, "hflip": camera.hflip})

    # ---------- 同步测试（U5） ----------

    @app.post("/dev/api/sync/pull")
    async def dev_sync_pull(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        triggered = handle.runtime.sync_pull()
        logger.info("dev event=sync_pull mode=%s", triggered)
        sync = dict(getattr(handle.runtime.status(), "sync", {}))
        return JSONResponse({"triggered": triggered, "sync": sync})

    # ---------- 整机场景回放（U6） ----------

    @app.post("/dev/api/replay")
    async def dev_replay(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body 须为合法 JSON 对象"}, status_code=400)
        text = str(body.get("text", "")).strip()
        if not body.get("confirm"):
            return JSONResponse(
                {"error": "缺少确认标记（回放会真实出声并驱动状态机）"}, status_code=400
            )
        if not text:
            return JSONResponse({"error": "缺少指令文本"}, status_code=400)
        if handle.dev_mutex.locked():
            return JSONResponse({"error": "测试进行中，请稍后再试"}, status_code=409)
        rt = handle.runtime
        # 显式隐私预检（唤醒被引擎门拒时静默返回，不可等状态区分原因）
        if rt.privacy.is_on:
            return JSONResponse({"error": "隐私模式开启，唤醒被拒（回放不可用）"}, status_code=403)
        if rt.engine.state.value != "standby":
            return JSONResponse(
                {"error": f"引擎非待机（当前 {rt.engine.state.value}）"}, status_code=409
            )
        async with handle.dev_mutex:
            transitions_before = len(rt.engine.transitions)
            spoken_before = len(rt.recent_speaks)
            await rt.engine.on_wake()
            # 轮询 LISTENING（~2s；镜像 harness.wait_state）
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and rt.engine.state.value != "listening":
                await asyncio.sleep(0.05)
            if rt.engine.state.value != "listening":
                return JSONResponse(
                    {"error": "唤醒未落地（引擎未进入聆听）", "transitions": []},
                    status_code=504,
                )
            # 会话竞争守卫：真实唤醒词中途插入会使状态偏离，注入前复核
            if rt.engine.state.value != "listening":
                return JSONResponse({"error": "会话竞争，回放中止"}, status_code=409)
            await rt.engine.on_asr_text(text)
            # 等待回待机（整体上界 ~15s；LLM 路径可能更长——超时返回部分结果）
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and rt.engine.state.value != "standby":
                await asyncio.sleep(0.1)
        return JSONResponse({
            "transitions": rt.engine.transitions[transitions_before:],
            "spoken": list(rt.recent_speaks)[spoken_before:],
            "state": rt.engine.state.value,
        })

