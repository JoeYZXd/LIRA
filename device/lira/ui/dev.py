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


class DevHandle:
    """dev 控制台对运行时能力的访问面（弱耦合：持 runtime 引用按需取）。"""

    def __init__(self, runtime) -> None:  # DeviceRuntime（鸭子类型避免循环导入）
        self.runtime = runtime
        #: 变更类测试端点的互斥锁（录音/ASR/视觉/回放；进行中 → 409）
        self.dev_mutex: asyncio.Lock = asyncio.Lock()

    @property
    def store(self) -> ApplianceStore:
        return self.runtime.store

    @property
    def vault(self) -> PassphraseVault:
        return self.runtime.vault


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
        denied = _require_session(request, api=False)
        if denied is not None:
            return denied
        return HTMLResponse(_dev_page_html())

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

    # ---------- U3+/U4+/U5+/U6+ 的端点在后续单元追加 ----------


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
  <div class="row"><button id="rec-btn" onclick="record()">录音 10 秒</button>
    <span id="rec-out" class="muted"></span>
    <audio id="clip" controls style="display:none"></audio></div>
  <div class="row"><button onclick="asrTest()">识别上一步录音（ASR→意图）</button>
    <span id="asr-out"></span></div>
</section>

<section id="sec-vision">
  <h2>视觉链路测试</h2>
  <div class="row"><button onclick="visionTest()">拍摄 + OCR</button>
    <span id="vis-out"></span></div>
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
async function record() {
  const btn = document.getElementById('rec-btn');
  btn.disabled = true;
  for (let left = 10; left > 0; left--) { out('rec-out', '录音中…剩 ' + left + 's'); await new Promise(res => setTimeout(res, 1000)); }
  const r = await api('/dev/api/record', {method: 'POST', body: '{}'});
  btn.disabled = false;
  if (r.status !== 200) return out('rec-out', r.body.error || r.status, 'err');
  out('rec-out', '完成', 'ok');
  const clip = document.getElementById('clip');
  clip.src = '/dev/api/clip?ts=' + Date.now(); clip.style.display = 'block'; clip.play();
}
async function asrTest() {
  const r = await api('/dev/api/asr', {method: 'POST', body: '{}'});
  out('asr-out', r.status === 200 ? ('意图: ' + JSON.stringify(r.body.intent) + ' 文本: ' + r.body.text) : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}
async function visionTest() {
  const r = await api('/dev/api/vision', {method: 'POST', body: '{}'});
  if (r.status !== 200) return out('vis-out', r.body.error || r.status, 'err');
  document.getElementById('vis-view').textContent = r.body.text || '（无文字）';
  out('vis-out', '耗时: ' + JSON.stringify(r.body.timings_ms), 'ok');
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
