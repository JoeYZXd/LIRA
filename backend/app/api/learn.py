"""红外学习 API（U8，R32/AE5）：后台发起学习 + 设备回传码值入库。

流程：管理端 POST /admin/learn 落一条 pending 学习请求 → 设备 WS 连接的
轮询循环领取并下发 `LearnStartMsg` → 设备录一帧原始码 → 已认证 WS 回传
`LearnResultMsg` → 后台码值入库（UPSERT）且 version+1 → 快照推送。
学习发起本身落库即返回（异步下发），状态经 GET /admin/learn/{id} 轮询
——这与 UI 的「学习按钮 → 等待设备按键」交互一致。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.devices import admin_and_csrf
from app.models import Database

router = APIRouter()


@router.post("/admin/learn")
async def start_learn(request: Request, claims: dict = Depends(admin_and_csrf),
                      device: str = Form(...), action: str = Form(...)):
    """发起学习。`device` 为家电名（协议 LearnStartMsg.device 语义），
    请求由任意已连接硬件设备的 WS 轮询循环领取并下发。"""
    db: Database = request.app.state.db
    device = device.strip()
    action = action.strip()
    if db.get_appliance(device) is None or not action:
        return JSONResponse({"error": "invalid_learn_request"}, status_code=400)
    learn_id = uuid.uuid4().hex
    db.create_learn(learn_id, device, action)
    return RedirectResponse(f"/appliances/{device}?learn_id={learn_id}", status_code=303)


@router.get("/admin/learn/{learn_id}")
async def learn_status(learn_id: str, request: Request, claims: dict = Depends(admin_and_csrf)):
    """学习请求状态（轮询面）：pending / sent / done(+code|error)。"""
    row = request.app.state.db.get_learn(learn_id)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({
        "learn_id": row["learn_id"],
        "device": row["device"],
        "action": row["action"],
        "status": row["status"],
        "has_code": row["code"] is not None,
        "error": row["error"],
    })
