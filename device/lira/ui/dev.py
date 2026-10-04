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
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lira.appliances.store import ApplianceStore
from lira.privacy import PassphraseVault

__all__ = ["DevHandle", "mount_dev", "reset_dev_session_secret", "SESSION_COOKIE"]

logger = logging.getLogger("lira.dev")

SESSION_COOKIE = "lira_dev_session"
_SESSION_TTL_SECONDS = 4 * 3600
_SECRET_META_KEY = "dev_console_session_secret"
_SALT = "lira-dev-session"

_MAX_FAILED = 5
_LOCK_SECONDS = 300.0
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: 录音内存缓冲（零落盘；进程内，覆盖复用）
clip_state: dict[str, bytes | None] = {"wav": None, "ts": 0.0}


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


@dataclass
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


def mount_dev(app: FastAPI, handle: DevHandle) -> None:
    """向设备 UI 应用挂载 dev 路由（仅 dev_console.enabled 时由 create_app 调用）。"""
    limiter = _RateLimiter()

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
            return HTMLResponse(_dev_page_html())
        if not handle.vault.is_set():
            # 首启引导：口令未设（fail-closed，去设备 UI 的设置页）
            return RedirectResponse("/passphrase/setup", status_code=303)
        # 口令已设：返回控制台专属登录页（浏览器唯一登录入口）
        return HTMLResponse(_dev_login_html())

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
        body = await request.json()
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
        s = handle.runtime.status()
        s["sync_detail"] = dict(handle.runtime.sync_status)
        return JSONResponse(s)

    @app.get("/dev/api/logs")
    async def dev_logs(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        from lira.main import RING_LOG_HANDLER

        return JSONResponse({"lines": RING_LOG_HANDLER.snapshot()[-100:]})

    @app.post("/dev/api/loglevel")
    async def dev_loglevel(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        body = await request.json()
        level = str(body.get("level", "")).upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            return JSONResponse({"error": "级别须为 DEBUG/INFO/WARNING/ERROR"}, status_code=400)
        logging.getLogger("lira").setLevel(getattr(logging, level))
        logger.info("dev event=loglevel level=%s", level)
        return JSONResponse({"ok": True, "level": level})

    # ---------- 语音链路测试（U3） ----------

    @app.post("/dev/api/tts")
    async def dev_tts(request: Request):
        denied = _require_session(request, api=True)
        if denied is not None:
            return denied
        body = await request.json()
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
        body = await request.json()
        rotation = body.get("rotation")
        hflip = body.get("hflip")
        if rotation is not None:
            if rotation not in (0, 90, 180, 270):
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
        body = await request.json()
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


def _dev_login_html() -> str:
    """控制台登录页（口令已设时的 /dev 未鉴权着陆页；fetch 提交）。"""
    return """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>LIRA 开发者控制台 - 登录</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;display:flex;align-items:center;
      justify-content:center;height:100vh;background:#fafafa}
 .card{border:1px solid #ccc;border-radius:10px;padding:28px;background:#fff;width:300px}
 input{width:100%;padding:8px;margin:8px 0;box-sizing:border-box}
 button{width:100%;padding:8px} .err{color:#c00;font-size:13px;min-height:18px}
 h1{font-size:17px;margin:0 0 12px}
</style></head><body>
<div class="card">
 <h1>LIRA 开发者控制台</h1>
 <input id="pass" type="password" placeholder="设备口令" onkeydown="if(event.key==='Enter')doLogin()">
 <button onclick="doLogin()">登录</button>
 <div id="err" class="err"></div>
 <p class="muted" style="font-size:12px;color:#666">首次使用请先在 <a href="/passphrase/setup">设备面板</a> 设置口令</p>
</div>
<script>
async function doLogin() {
  const r = await fetch('/dev/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({passphrase: document.getElementById('pass').value})
  });
  if (r.ok) { location.href = '/dev'; return; }
  const j = await r.json().catch(() => ({error: 'HTTP ' + r.status}));
  document.getElementById('err').textContent = j.error || ('HTTP ' + r.status);
}
</script>
</body></html>"""


def _dev_page_html() -> str:
    """v1 单页（fetch JSON + 原生 JS；结果渲染无构建链）。"""
    return """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>LIRA 开发者控制台</title>
<style>
 body{font-family:system-ui,sans-serif;margin:16px;max-width:920px;background:#fafafa}
 section{border:1px solid #ccc;border-radius:8px;padding:12px;margin-bottom:12px;background:#fff}
 h2{margin:4px 0 8px;font-size:16px} button{margin:4px;padding:6px 12px}
 pre{background:#f6f6f6;padding:8px;white-space:pre-wrap;max-height:280px;overflow:auto}
 input,select{margin:4px;padding:4px} .row{margin:6px 0}
 .muted{color:#666;font-size:13px} .ok{color:#0a0} .err{color:#c00}
</style></head><body>
<h1>LIRA 开发者控制台</h1>

<section id="sec-status">
  <h2>整机状态</h2>
  <div class="row"><button onclick="loadStatus()">刷新状态</button>
    <span id="status-out" class="muted"></span></div>
  <pre id="status-view">（未加载）</pre>
</section>

<section id="sec-settings">
  <h2>运行参数</h2>
  <div class="row">音量 <input id="vol" type="number" step="0.1" min="0.1" max="2">
    <button onclick="postForm('/volume','vol')">设置音量</button>
    语速 <input id="spd" type="number" step="0.1" min="0.5" max="2">
    <button onclick="postForm('/speed','spd')">设置语速</button>
    <span id="set-out"></span></div>
  <div class="row">日志级别
    <select id="lvl">
      <option>DEBUG</option><option selected>INFO</option>
      <option>WARNING</option><option>ERROR</option>
    </select>
    <button onclick="setLevel()">调整</button><span id="lvl-out"></span></div>
</section>

<section id="sec-logs">
  <h2>运行日志（最近 100 条）</h2>
  <div class="row"><button onclick="loadLogs()">刷新日志</button><span id="log-out"></span></div>
  <pre id="log-view">（未加载）</pre>
</section>

<section id="sec-voice">
  <h2>语音链路测试</h2>
  <div class="row">TTS 文本（POST body）：<input id="tts-text" size="40">
    <button onclick="ttsSpeak()">播报</button><span id="tts-out"></span></div>
  <div class="row">
    <button id="rec-btn" onclick="recordToggle()">开始录音</button>
    <span id="rec-out" class="muted">录音期间主循环语音暂停（独占设备）</span>
    <audio id="clip" controls style="display:none"></audio></div>
  <div class="row"><button onclick="asrTest()">识别上一步录音（ASR→意图）</button>
    <span id="asr-out"></span></div>
</section>

<section id="sec-vision">
  <h2>视觉链路测试</h2>
  <div class="row">
    <button id="preview-btn" onclick="previewToggle()">开启实时预览（~0.5fps）</button>
    <span id="preview-out" class="muted">慢速单帧路径预览；隐私开启时取帧失败即停</span></div>
  <img id="preview-img" style="max-width:640px;display:none;border:1px solid #ddd" alt="preview">
  <div class="row" style="margin-top:8px">
    <button onclick="orient(90)">旋转 90°</button>
    <button onclick="orient(0)">复位</button>
    <button onclick="flip()">水平翻转</button>
    <span id="orient-out" class="muted"></span></div>
  <div class="row"><button onclick="visionTest()">拍摄 + OCR</button>
    <span id="vis-out"></span></div>
  <img id="vis-img" style="max-width:640px;display:none;border:1px solid #ddd" alt="capture">
  <pre id="vis-view"></pre>
</section>

<section id="sec-sync">
  <h2>同步</h2>
  <div class="row"><button onclick="syncPull()">立即拉取后台快照</button>
    <span id="sync-out"></span></div>
</section>

<section id="sec-replay">
  <h2>整机场景回放</h2>
  <div class="row">指令文本：<input id="replay-text" size="30" value="打开台灯">
    <button onclick="replay(true)">回放（真实出声）</button>
    <span id="replay-out"></span></div>
  <pre id="replay-view"></pre>
</section>

<script>
let recording = false, previewOn = false, previewTimer = null, fetching = false;
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {'Content-Type': 'application/json'}}, opts||{}));
  const t = await r.text();
  let j; try { j = JSON.parse(t); } catch { j = {raw: t}; }
  return {status: r.status, body: j};
}
function out(id, msg, cls) {
  const el = document.getElementById(id);
  el.textContent = msg; el.className = cls || 'muted';
}
async function loadStatus() {
  const r = await api('/dev/api/status');
  if (r.status !== 200) return out('status-out', '加载失败: ' + r.status, 'err');
  const s = r.body;
  document.getElementById('status-view').textContent = JSON.stringify(s, null, 2);
  document.getElementById('vol').value = s.privacy_on ? undefined : (s.volume === undefined ? undefined : undefined);
  out('status-out', '引擎=' + s.engine + ' 路由=' + s.route +
    (s.privacy_on ? ' [隐私开启：采集类测试将被拒绝]' : ''), 'ok');
}
async function loadLogs() {
  const r = await api('/dev/api/logs');
  if (r.status !== 200) return out('log-out', '加载失败: ' + r.status, 'err');
  document.getElementById('log-view').textContent = (r.body.lines || []).join('\\n') || '（空）';
  out('log-out', r.body.lines.length + ' 条', 'ok');
}
async function postForm(path, inputId) {
  const fd = new FormData();
  fd.append('value', document.getElementById(inputId).value);
  const r = await fetch(path, {method: 'POST', body: fd});
  out('set-out', r.ok ? '已提交' : '失败: ' + r.status, r.ok ? 'ok' : 'err');
}
async function setLevel() {
  const r = await api('/dev/api/loglevel', {method: 'POST', body: JSON.stringify({level: document.getElementById('lvl').value})});
  out('lvl-out', r.status === 200 ? '已调整' : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}
async function ttsSpeak() {
  const text = document.getElementById('tts-text').value;
  const r = await api('/dev/api/tts', {method: 'POST', body: JSON.stringify({text})});
  out('tts-out', r.status === 200 ? ('已播报 ' + r.body.elapsed_ms + 'ms') : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}
async function recordToggle() {
  const btn = document.getElementById('rec-btn');
  if (!recording) {
    const r = await api('/dev/api/record/start', {method: 'POST', body: '{}'});
    if (r.status !== 200) return out('rec-out', r.body.error || r.status, 'err');
    recording = true;
    btn.textContent = '结束录音';
    recLeft = 0;
    recTimer = setInterval(() => { recLeft += 1; out('rec-out', '录音中… ' + recLeft + 's（上限 60s）'); }, 1000);
    out('rec-out', '录音中…', 'ok');
  } else {
    clearInterval(recTimer);
    const r = await api('/dev/api/record/stop', {method: 'POST', body: '{}'});
    recording = false;
    btn.textContent = '开始录音';
    if (r.status !== 200) return out('rec-out', r.body.error || r.status, 'err');
    if (r.body.discarded) return out('rec-out', '已按隐私联锁丢弃本次录音', 'err');
    out('rec-out', '完成 ' + r.body.seconds + 's / ' + r.body.bytes + 'B', 'ok');
    const clip = document.getElementById('clip');
    clip.src = '/dev/api/clip?ts=' + Date.now(); clip.style.display = 'block'; clip.play();
  }
}
async function asrTest() {
  const r = await api('/dev/api/asr', {method: 'POST', body: '{}'});
  out('asr-out', r.status === 200 ? ('文本: ' + r.body.text + ' | 意图: ' + JSON.stringify(r.body.intent)) : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}
async function orient(rotation) {
  const body = rotation !== null ? {rotation} : {hflip: null};
  const payload = rotation === null ? {hflip: !flipped} : {rotation};
  const r = await api('/dev/api/camera/orientation', {method: 'POST', body: JSON.stringify(payload)});
  if (r.status !== 200) return out('orient-out', r.body.error || r.status, 'err');
  flipped = !!r.body.hflip;
  out('orient-out', '方向: ' + r.body.rotation + '°' + (flipped ? '（已翻转）' : ''), 'ok');
  if (previewOn) { await frame(); }
}
let flipped = false;
async function visionTest() {
  out('vis-out', '拍摄识别中…（慢速单帧，约数秒）');
  const r = await api('/dev/api/vision', {method: 'POST', body: '{}'});
  if (r.status !== 200) return out('vis-out', r.body.error || r.status, 'err');
  const img = document.getElementById('vis-img');
  img.src = r.body.image; img.style.display = 'block';
  document.getElementById('vis-view').textContent = r.body.text || '（无文字）';
  out('vis-out', '耗时: ' + JSON.stringify(r.body.timings_ms), 'ok');
}
async function previewToggle() {
  const btn = document.getElementById('preview-btn');
  if (previewOn) {
    previewOn = false; clearInterval(previewTimer);
    btn.textContent = '开启实时预览（~0.5fps）';
    document.getElementById('preview-img').style.display = 'none';
    return out('preview-out', '预览已停止');
  }
  previewOn = true;
  btn.textContent = '停止预览';
  const frame = async () => {
    const r = await fetch('/dev/api/vision/frame?ts=' + Date.now());
    if (r.status === 200) {
      const blob = await r.blob();
      const img = document.getElementById('preview-img');
      img.src = URL.createObjectURL(blob); img.style.display = 'block';
      out('preview-out', '预览中…');
    } else {
      previewOn = false; clearInterval(previewTimer);
      btn.textContent = '开启实时预览（~0.5fps）';
      out('preview-out', '取帧失败已停止: ' + r.status, 'err');
    }
  };
  await frame();
  previewTimer = setInterval(async () => {
    if (!previewOn || fetching) return;
    fetching = true;
    try { await frame(); } finally { fetching = false; }
  }, 2200);
}
async function syncPull() {
  const r = await api('/dev/api/sync/pull', {method: 'POST', body: '{}'});
  out('sync-out', r.status === 200 ? ('状态: ' + JSON.stringify(r.body.sync)) : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}
async function replay(confirm) {
  const text = document.getElementById('replay-text').value;
  const r = await api('/dev/api/replay', {method: 'POST', body: JSON.stringify({text, confirm})});
  if (r.status !== 200) return out('replay-out', r.body.error || r.status, 'err');
  document.getElementById('replay-view').textContent =
    '迁移: ' + (r.body.transitions || []).join(' -> ') + '\\n播报: ' + (r.body.spoken || []).join(' | ');
  out('replay-out', '完成', 'ok');
}
loadStatus();
</script>
</body></html>"""
