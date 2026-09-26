"""设备 API（U8）：管理端 CRUD/配对页 + 设备 HTTP 通道 + 设备 WS 通道。

鉴权双通道（Key Decisions）：
  - 管理端路由：cookie 会话 + CSRF（POST 一律校验）；
  - 设备 HTTP：`X-Device-Token` header（哈希查库）；
  - 设备 WS：连接后**首帧**必须 hello 出示 token，未认证不处理任何消息
    （协议 `HelloMsg`/`AuthOkMsg`/`AuthErrorMsg`）。

配置推送模型（R30）：安全关键变更后台落库 version+1 后，由 WS 连接自身
的轮询循环（0.5s）发现版本差并立即推送快照——同 loop 内发送，天然兼容
TestClient 与真实 uvicorn；60s 心跳拉取仅作离线兜底。设备回执经
`Database.record_ack` 入库留痕并驱动重配对收尾。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse

from app import auth
from app.auth import AuthError
from app.hub import DeviceHub
from app.models import Database
from app.protocol import (
    HelloMsg,
    LearnResultMsg,
    LearnStartMsg,
    ProtocolError,
    PullMsg,
    SnapshotAckMsg,
    SnapshotMsg,
    parse_frame,
)

logger = logging.getLogger("lira.backend")

router = APIRouter()

#: WS 轮询周期：配置推送/学习下发的发现延迟上限（R30 要求 60s 内，远小于其值）
WS_POLL_SECONDS = 0.5


# ---------- 依赖（带 CSRF 的管理端守卫） ----------

async def admin_and_csrf(request: Request) -> dict:
    claims = auth.require_admin(request)
    if request.method == "POST":
        form = await request.form()
        try:
            auth.check_csrf(request, form.get("_csrf"))
        except AuthError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return claims


def _auth_error_response(exc: AuthError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=401)


# ---------- 管理端：设备注册 / 吊销重发 / 配对 ----------

@router.post("/admin/devices")
async def create_device(request: Request, claims: dict = Depends(admin_and_csrf),
                        name: str = Form(...)):
    db: Database = request.app.state.db
    name = name.strip()
    if not name or db.get_device(name) is not None:
        return RedirectResponse(f"/?error=device_invalid#{name}", status_code=303)
    token = db.create_device(name)
    # token 明文只展示一次（库中仅哈希）
    response = RedirectResponse(f"/?token_once={token}", status_code=303)
    return response


@router.post("/admin/devices/{name}/revoke")
async def revoke_device(name: str, request: Request, claims: dict = Depends(admin_and_csrf)):
    db: Database = request.app.state.db
    if db.get_device(name) is None:
        return RedirectResponse("/", status_code=303)
    token = db.revoke_device_token(name)
    return RedirectResponse(f"/?token_once={token}", status_code=303)


@router.post("/admin/devices/{name}/pair")
async def pair_device(name: str, request: Request, claims: dict = Depends(admin_and_csrf),
                      pairing_code: str = Form(...)):
    """重配对（epoch 迁移唯一入口）：子女输入设备屏显一次性配对码。"""
    db: Database = request.app.state.db
    if db.get_device(name) is None:
        return RedirectResponse("/", status_code=303)
    code = pairing_code.strip()
    if not code.isdigit() or len(code) != 6:
        return RedirectResponse(f"/?error=bad_pairing_code", status_code=303)
    db.start_pairing(name, code)
    return RedirectResponse(f"/?paired=1", status_code=303)


# ---------- 设备 HTTP 通道（X-Device-Token） ----------

@router.get("/api/device/snapshot")
async def device_snapshot(request: Request, device: str = Depends(auth.require_device)):
    """全量配置快照（R30 离线兜底路径）：全量 + (epoch, version)，设备端幂等。"""
    db: Database = request.app.state.db
    db.touch_last_seen(device)
    snapshot = db.build_snapshot(device)
    return JSONResponse(snapshot.to_json())


@router.post("/api/device/ack")
async def device_ack(request: Request, device: str = Depends(auth.require_device)):
    """设备回执 (epoch, version) 入库留痕（含 HTTP 路径）。"""
    db: Database = request.app.state.db
    try:
        msg = SnapshotAckMsg.from_json(await request.json())
    except (ProtocolError, ValueError) as exc:
        return JSONResponse({"error": f"bad_ack: {exc}"}, status_code=400)
    db.record_ack(device, msg)
    return JSONResponse({"ok": True})


# ---------- 设备 WS 通道（首帧 token 鉴权） ----------

def _handle_learn_result(db: Database, device: str, msg: LearnResultMsg) -> None:
    """学习回传入库（AE5）：码值入库 + version+1；失败也留痕。"""
    row = db.finish_learn(msg.learn_id, msg.code, msg.error)
    if row is not None and msg.code:
        db.store_learned_code(row["device"], row["action"], msg.code)
        logger.info("学习完成: device=%s action=%s", row["device"], row["action"])


async def device_ws(ws: WebSocket) -> None:
    """设备 WS 会话：首帧鉴权 → 轮询循环（推送快照 / 下发学习 / 处理回执）。"""
    db: Database = ws.app.state.db
    hub: DeviceHub = ws.app.state.hub
    await ws.accept()

    # ---- 首帧鉴权：未认证不处理任何消息 ----
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close()
        return
    try:
        frame = parse_frame(first)
    except ProtocolError as exc:
        await ws.send_json({"type": "auth_error", "reason": f"protocol_error: {exc}"})
        await ws.close()
        return
    if not isinstance(frame, HelloMsg):
        await ws.send_json({"type": "auth_error", "reason": "first_frame_must_be_hello"})
        await ws.close()
        return
    device = db.find_device_by_token(frame.token)
    if device is None:
        await ws.send_json({"type": "auth_error", "reason": "invalid_token"})
        await ws.close()
        return
    await ws.send_json({"type": "auth_ok"})
    db.touch_last_seen(device)
    hub.connect(device)
    logger.info("设备已连接: %s", device)

    pushed: tuple[int, int] | None = None  # 本连接已推送过的 (epoch, version)
    pairing_sent: str | None = None  # 已推送过配对快照的配对码（每码只推一次）

    async def maybe_push() -> tuple[int, int] | None:
        """推送纪律：每个 (epoch, version) 每连接只推一次（重连重推，
        设备端幂等跳过）；重配对快照（含 pairing_code/new token）每码只推一次。"""
        nonlocal pushed, pairing_sent
        current = (db.epoch(), db.version())
        pairing = db.pairing_code_for(device)
        if pairing is not None and pairing != pairing_sent:
            await ws.send_json(db.build_snapshot(device).to_json())
            pairing_sent = pairing
            pushed = current
        elif pushed != current:
            await ws.send_json(db.build_snapshot(device).to_json())
            pushed = current
        return pushed

    async def service_pending_learns() -> None:
        for row in db.claim_pending_learns():
            await ws.send_json(LearnStartMsg(
                learn_id=row["learn_id"], device=row["device"], action=row["action"],
            ).to_json())

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_json(), timeout=WS_POLL_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                await service_pending_learns()
                pushed = await maybe_push()
                continue
            try:
                frame = parse_frame(raw)
            except ProtocolError as exc:
                logger.warning("非法帧（已丢弃）: %s", exc)
                continue
            if isinstance(frame, PullMsg):
                # 心跳拉取兜底：设备落后才回快照，否则静默（省流量）。
                # 无论是否下发，均视为设备已知晓当前版本（抑制连接初期的重复推送）
                current = (db.epoch(), db.version())
                if (frame.epoch, frame.version) != current:
                    await ws.send_json(db.build_snapshot(device).to_json())
                pushed = current
            elif isinstance(frame, SnapshotAckMsg):
                db.record_ack(device, frame)
            elif isinstance(frame, LearnResultMsg):
                _handle_learn_result(db, device, frame)
            else:
                logger.debug("忽略消息类型: %s", type(frame).__name__)
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(device)
        logger.info("设备断开: %s", device)
