"""家电/场景/设置管理 API（U8）：R7/R14/R15 配置面 + 隐私远程开关。

所有变更原子落库并 version+1；安全关键变更（高危标记、禁用、隐私切换）
落库后由设备 WS 轮询循环立即推送（`api.devices.device_ws`）。
写路径一律 cookie 会话 + CSRF 双认证（配置写入必须双向认证，防伪造
"禁用"指令）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.api.devices import admin_and_csrf
from app.models import Database

router = APIRouter()


def _parse_triggers(raw: str) -> tuple[str, ...]:
    return tuple(t.strip() for t in raw.replace("，", ",").split(",") if t.strip())


def _parse_aliases(raw: str) -> tuple[str, ...]:
    return _parse_triggers(raw)


# ---------- 家电 CRUD ----------

@router.post("/admin/appliances")
async def upsert_appliance(
    request: Request,
    claims: dict = Depends(admin_and_csrf),
    name: str = Form(...),
    aliases: str = Form(""),
    action: str = Form(""),
    triggers: str = Form(""),
    is_high_risk: str = Form(""),
    enabled: str = Form(""),
):
    db: Database = request.app.state.db
    name = name.strip()
    if not name:
        return RedirectResponse("/?error=appliance_invalid", status_code=303)
    actions: dict[str, tuple[str, ...]] = {}
    if action.strip():
        actions[action.strip()] = _parse_triggers(triggers)
    # checkbox 语义：勾选="on"，未勾选=缺省。开关类局部变更走专门端点
    # （toggle_high_risk / toggle_enabled），本表单字段缺省时保留现值。
    existing = db.get_appliance(name)
    db.upsert_appliance(
        name=name,
        aliases=_parse_aliases(aliases) or (name,),
        actions=actions,
        is_high_risk=is_high_risk == "on" if existing is None
        else (is_high_risk == "on" if is_high_risk else bool(existing["is_high_risk"])),
        # 新建默认启用（禁用走 /enabled 专用端点）；编辑缺省保留现值
        enabled=True if existing is None
        else (enabled == "on" if enabled else bool(existing["enabled"])),
    )
    return RedirectResponse(f"/appliances/{name}", status_code=303)


@router.post("/admin/appliances/{name}/delete")
async def delete_appliance(name: str, request: Request, claims: dict = Depends(admin_and_csrf)):
    db: Database = request.app.state.db
    if db.get_appliance(name) is not None:
        db.delete_appliance(name)
    return RedirectResponse("/", status_code=303)


@router.post("/admin/appliances/{name}/high_risk")
async def toggle_high_risk(name: str, request: Request, claims: dict = Depends(admin_and_csrf),
                           value: str = Form(...)):
    """高危标记开关（R6/R23 安全关键变更 → 立即推送）。"""
    db: Database = request.app.state.db
    row = db.get_appliance(name)
    if row is None:
        return RedirectResponse("/", status_code=303)
    high_risk = value == "on"
    db.upsert_appliance(name=name, aliases=tuple(json.loads(row["aliases"])),
                        actions={}, is_high_risk=high_risk, enabled=bool(row["enabled"]))
    return RedirectResponse(f"/appliances/{name}", status_code=303)


@router.post("/admin/appliances/{name}/enabled")
async def toggle_enabled(name: str, request: Request, claims: dict = Depends(admin_and_csrf),
                         value: str = Form(...)):
    """禁用/启用开关（R7 安全关键变更 → 立即推送；AE7 主路径）。"""
    db: Database = request.app.state.db
    row = db.get_appliance(name)
    if row is None:
        return RedirectResponse("/", status_code=303)
    enabled = value == "on"
    db.upsert_appliance(name=name, aliases=tuple(json.loads(row["aliases"])),
                        actions={}, is_high_risk=bool(row["is_high_risk"]), enabled=enabled)
    return RedirectResponse(f"/appliances/{name}", status_code=303)


# ---------- 场景 CRUD ----------

@router.post("/admin/scenes")
async def upsert_scene(request: Request, claims: dict = Depends(admin_and_csrf),
                       name: str = Form(...), steps: str = Form("")):
    """场景 = 设备动作列表；steps 每行一条 `设备:动作`。"""
    db: Database = request.app.state.db
    name = name.strip()
    if not name:
        return RedirectResponse("/", status_code=303)
    parsed: list[tuple[str, str]] = []
    for line in steps.splitlines():
        line = line.strip()
        if not line:
            continue
        device, _, action = line.partition(":")
        if device.strip() and action.strip():
            parsed.append((device.strip(), action.strip()))
    db.upsert_scene(name, parsed)
    return RedirectResponse("/", status_code=303)


@router.post("/admin/scenes/{name}/delete")
async def delete_scene(name: str, request: Request, claims: dict = Depends(admin_and_csrf)):
    db: Database = request.app.state.db
    db.delete_scene(name)
    return RedirectResponse("/", status_code=303)


# ---------- 设置（隐私远程开关，R30/R14） ----------

@router.post("/admin/settings/privacy")
async def set_privacy(request: Request, claims: dict = Depends(admin_and_csrf),
                      value: str = Form(...)):
    """隐私模式远程开关（安全关键变更 → 立即推送）。"""
    db: Database = request.app.state.db
    db.set_setting("privacy_mode", value == "on")
    return RedirectResponse("/", status_code=303)
