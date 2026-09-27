"""LIRA 子女管理后台（U8）：FastAPI 入口与应用工厂。

安全面（Key Technical Decisions / U8 Approach）：
  - 管理员凭据**首启引导**：库中无管理员时一切请求强制进入 /setup，
    系统不存在空/默认密码状态；
  - 会话 cookie：HttpOnly + SameSite=Strict（LAN 明文 HTTP，Phase 1 不加
    Secure——该暴露面已知限制记录于 deploy/SETUP.md）；
  - CSRF：签名 cookie + 表单回传 double-submit，POST 管理路由一律校验；
  - 设备通道与配置同步见 `app.api.devices`。

页面（Jinja2，纯表单无 Node 构建链）：设备列表（在线/最后心跳/配置版本）、
家电配置表单、高危标记开关、学习按钮、隐私开关。
"""

from __future__ import annotations

import json
import pathlib
import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth
from app.api import appliances as appliances_api
from app.api import devices as devices_api
from app.api import learn as learn_api
from app.auth import AuthError
from app.hub import DeviceHub
from app.models import Database

__all__ = ["create_app"]

TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DEFAULT_DB_PATH = "data/lira-backend.db"


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    """模板渲染：统一注入 csrf token 与管理员名。缺 csrf cookie 时补发。

    CSRF 一致性纪律：页面内嵌的 token 必须与 cookie 中已有值一致
    （double-submit 比对的是 cookie 值），否则提交即 403。
    """
    db: Database = request.app.state.db
    claims = auth.read_session(db, request.cookies.get(auth.SESSION_COOKIE))
    existing_csrf = request.cookies.get(auth.CSRF_COOKIE)
    csrf_token = existing_csrf or auth.issue_csrf(db)
    ctx = {"request": request, "csrf_token": csrf_token,
           "admin_name": claims["sub"] if claims else None}
    ctx.update(context or {})
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if not existing_csrf:
        response.set_cookie(auth.CSRF_COOKIE, csrf_token,
                            httponly=True, samesite="strict")
    return response


def create_app(db_path: str = DEFAULT_DB_PATH) -> FastAPI:
    """应用工厂（测试为每个用例建独立临时库）。"""
    app = FastAPI(title="LIRA 子女管理后台", docs_url=None, redoc_url=None)
    app.state.db = Database(db_path)
    app.state.hub = DeviceHub()

    app.include_router(devices_api.router)
    app.include_router(appliances_api.router)
    app.include_router(learn_api.router)
    app.add_api_websocket_route("/ws/device", devices_api.device_ws)

    # ---------- 首启引导 ----------

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        db: Database = request.app.state.db
        if db.has_admin():
            return RedirectResponse("/login", status_code=303)
        return render(request, "setup.html")

    @app.post("/setup")
    async def setup_submit(request: Request, username: str = Form(...),
                           password: str = Form(...)):
        db: Database = request.app.state.db
        if db.has_admin():
            return RedirectResponse("/login", status_code=303)
        if len(username.strip()) < 2 or len(password) < 8:
            return render(request, "setup.html",
                          {"error": "用户名至少 2 字符，密码至少 8 位"}, status_code=400)
        db.create_admin(username.strip(), auth.hash_password(password))
        response = RedirectResponse("/", status_code=303)
        _login_cookies(db, response, username.strip())
        return response

    # ---------- 登录 / 登出 ----------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        db: Database = request.app.state.db
        if not db.has_admin():
            return RedirectResponse("/setup", status_code=303)
        return render(request, "login.html", {"error": request.query_params.get("error")})

    @app.post("/login")
    async def login_submit(request: Request, username: str = Form(...),
                           password: str = Form(...)):
        db: Database = request.app.state.db
        if not auth.verify_login(db, username.strip(), password):
            remaining = int(auth.login_locked(db))
            msg = "用户名或密码错误" if remaining == 0 else f"失败次数过多，锁定 {remaining} 秒"
            return RedirectResponse(f"/login?error={msg}", status_code=303)
        response = RedirectResponse("/", status_code=303)
        _login_cookies(db, response, username.strip())
        return response

    @app.post("/logout")
    async def logout(request: Request):
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)
        response.delete_cookie(auth.CSRF_COOKIE)
        return response

    def _login_cookies(db: Database, response: RedirectResponse, username: str) -> None:
        response.set_cookie(auth.SESSION_COOKIE, auth.create_session(db, username),
                            httponly=True, samesite="strict", max_age=auth.SESSION_TTL_SECONDS)
        response.set_cookie(auth.CSRF_COOKIE, auth.issue_csrf(db),
                            httponly=True, samesite="strict", max_age=auth.SESSION_TTL_SECONDS)

    # ---------- 页面 ----------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        db: Database = request.app.state.db
        if not db.has_admin():
            return RedirectResponse("/setup", status_code=303)
        claims = auth.read_session(db, request.cookies.get(auth.SESSION_COOKIE))
        if claims is None:
            return RedirectResponse("/login", status_code=303)
        hub: DeviceHub = request.app.state.hub
        import time as _time

        devices = [
            {
                "name": row["name"],
                "online": hub.is_online(row["name"]) or _time.time() - row["last_seen"] < 150,
                "last_seen": row["last_seen"],
                "ack_epoch": row["ack_epoch"],
                "ack_version": row["ack_version"],
            }
            for row in db.list_devices()
        ]
        return render(request, "dashboard.html", {
            "devices": devices,
            "appliances": [dict(row) for row in db.list_appliances()],
            "scenes": db.list_scenes(),
            "epoch": db.epoch(),
            "version": db.version(),
            "privacy_mode": db.settings_all().get("privacy_mode", False),
            "token_once": request.query_params.get("token_once"),
            "error": request.query_params.get("error"),
        })

    @app.get("/appliances/{name}", response_class=HTMLResponse)
    async def appliance_page(name: str, request: Request):
        db: Database = request.app.state.db
        claims = auth.read_session(db, request.cookies.get(auth.SESSION_COOKIE))
        if claims is None:
            return RedirectResponse("/login", status_code=303)
        row = db.get_appliance(name)
        if row is None:
            raise StarletteHTTPException(status_code=404)
        return render(request, "appliance.html", {
            "appliance": dict(row),
            "aliases": ", ".join(json.loads(row["aliases"])),
            "actions": [dict(r) for r in db.appliance_actions(name)],
            "learn_id": request.query_params.get("learn_id"),
            "error": request.query_params.get("error"),
        })

    # ---------- 改密码（撤销计数 +1 → 全体会话失效） ----------

    async def _admin_csrf_dep(request: Request) -> dict:
        return await devices_api.admin_and_csrf(request)

    @app.post("/admin/password")
    async def change_password(request: Request, claims: dict = Depends(_admin_csrf_dep),
                              old_password: str = Form(...), new_password: str = Form(...)):
        db: Database = request.app.state.db
        admin = db.get_admin()
        if admin is None or not auth.verify_password(old_password, admin["password_hash"]):
            return RedirectResponse("/?error=password_wrong", status_code=303)
        if len(new_password) < 8:
            return RedirectResponse("/?error=password_short", status_code=303)
        db.change_password(auth.hash_password(new_password))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)  # 旧会话已随撤销计数失效
        return response

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError):
        if request.url.path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": str(exc)}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    return app


def main() -> None:  # pragma: no cover - 手动启动入口
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":  # pragma: no cover
    main()
