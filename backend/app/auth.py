"""后台鉴权（U8）：管理员 cookie 会话 + CSRF，设备 token 双通道。

Key Technical Decisions 落地：
  - 管理员会话用 **cookie**（HttpOnly + SameSite=Strict）而非 localStorage
    JWT——HTMX/表单页面无 JS 管 token，cookie 天然防 XSS 窃取；
  - 会话凭据为 PyJWT 签名 token，claims 带撤销计数（rev）：改密码/吊销使
    全体旧会话失效（核心不变量：不改密码就不能伪造旧会话）；
  - CSRF：itsdangerous 签名 token 下发独立 cookie，POST 表单须回传同值
    （double-submit + 签名防篡改），SameSite=Strict 为第二道闸；
  - 登录失败限速：连续 5 次失败锁 5 分钟（库内持久计数，重启不清零）；
  - 设备通道：`X-Device-Token` header，库内只比对 sha256 哈希（常量时间）；
  - 密码哈希 bcrypt（首选取手）；环境无 bcrypt 时回退 hashlib.scrypt
    （同为本单元可接受，格式化前缀区分，verify 按前缀路由）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

import jwt as pyjwt
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request

from app.models import Database

__all__ = [
    "AuthError",
    "hash_password",
    "verify_password",
    "create_session",
    "read_session",
    "issue_csrf",
    "check_csrf",
    "verify_login",
    "login_locked",
    "require_admin",
    "require_device",
]

SESSION_COOKIE = "lira_session"
CSRF_COOKIE = "lira_csrf"
SESSION_TTL_SECONDS = 12 * 3600
MAX_FAILED_LOGINS = 5
LOCK_SECONDS = 300.0


class AuthError(Exception):
    """鉴权失败（调用方转为 401/重定向）。"""


# ---------- 密码哈希（bcrypt 优先，scrypt 兜底） ----------

try:
    import bcrypt as _bcrypt  # type: ignore

    _HAVE_BCRYPT = True
except ImportError:  # pragma: no cover - 仅在无 bcrypt 环境生效
    _HAVE_BCRYPT = False


def hash_password(password: str) -> str:
    if _HAVE_BCRYPT:
        return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("scrypt$"):
        try:
            _, salt_hex, digest_hex = stored.split("$", 2)
            digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                    n=2**14, r=8, p=1)
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False
    if not _HAVE_BCRYPT:
        return False
    try:
        return _bcrypt.checkpw(password.encode(), stored.encode())
    except ValueError:
        return False


# ---------- 管理员会话（JWT + 撤销计数） ----------

def _session_secret(db: Database) -> bytes:
    """JWT 签名密钥：首启 setup 时生成并入库（重启不失效）。"""
    secret = db.get_meta("secret_key")
    if secret is None:
        secret = secrets.token_urlsafe(48)
        db.set_meta("secret_key", secret)
    return secret.encode()


def create_session(db: Database, username: str) -> str:
    admin = db.get_admin()
    if admin is None:
        raise AuthError("管理员未初始化")
    now = int(time.time())
    return pyjwt.encode(
        {"sub": username, "rev": admin["revoked_count"], "iat": now,
         "exp": now + SESSION_TTL_SECONDS},
        _session_secret(db),
        algorithm="HS256",
    )


def read_session(db: Database, token: str | None) -> dict[str, Any] | None:
    """校验会话：签名/过期/用户名/撤销计数任一不符即 None（fail-closed）。"""
    if not token:
        return None
    try:
        claims = pyjwt.decode(token, _session_secret(db), algorithms=["HS256"])
    except pyjwt.PyJWTError:
        return None
    admin = db.get_admin()
    if admin is None or claims.get("sub") != admin["username"]:
        return None
    if claims.get("rev") != admin["revoked_count"]:
        return None  # 改密码/吊销后旧会话全体失效
    return claims


def require_admin(request: Request) -> dict[str, Any]:
    """FastAPI 依赖：管理员会话校验；页面路由失败重定向 /login，
    JSON API 失败 401（按路径前缀区分）。"""
    db: Database = request.app.state.db
    claims = read_session(db, request.cookies.get(SESSION_COOKIE))
    if claims is None:
        if request.url.path.startswith("/api/"):
            raise AuthError("unauthorized")
        from fastapi import HTTPException
        from starlette.status import HTTP_303_SEE_OTHER

        raise HTTPException(status_code=HTTP_303_SEE_OTHER,
                            headers={"Location": "/login"})
    return claims


# ---------- CSRF（double-submit + 签名 cookie） ----------

def _csrf_serializer(db: Database) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_session_secret(db), salt="lira-csrf")


def issue_csrf(db: Database) -> str:
    return _csrf_serializer(db).dumps(secrets.token_urlsafe(24))


def check_csrf(request: Request, submitted: str | None) -> None:
    """CSRF 校验（double-submit + 签名 cookie）：
    ① cookie 值须为本后台签发的合法 token（防伪造 cookie）；
    ② 表单/头回传值与 cookie 原值一致（常量时间比较）。"""
    cookie_value = request.cookies.get(CSRF_COOKIE)
    if not cookie_value or not submitted:
        raise AuthError("missing_csrf")
    db: Database = request.app.state.db
    try:
        _csrf_serializer(db).loads(cookie_value, max_age=SESSION_TTL_SECONDS)
    except BadSignature as exc:
        raise AuthError("bad_csrf") from exc
    if not hmac.compare_digest(cookie_value, submitted):
        raise AuthError("csrf_mismatch")


# ---------- 登录（限速） ----------

def login_locked(db: Database) -> float:
    """返回剩余锁定秒数（0 = 未锁）。"""
    admin = db.get_admin()
    if admin is None:
        return 0.0
    remaining = admin["locked_until"] - time.time()
    return max(0.0, remaining)


def verify_login(db: Database, username: str, password: str) -> bool:
    """验证凭据并维护失败计数（5 次锁 5 分钟，U8 Approach）。"""
    if login_locked(db) > 0:
        return False
    admin = db.get_admin()
    if admin is not None and username == admin["username"] \
            and verify_password(password, admin["password_hash"]):
        db.record_login_success()
        return True
    db.record_login_failure()
    return False


# ---------- 设备通道（X-Device-Token） ----------

def require_device(request: Request) -> str:
    """FastAPI 依赖：设备 HTTP API 鉴权，返回设备名；失败 401。"""
    db: Database = request.app.state.db
    token = request.headers.get("X-Device-Token")
    name = db.find_device_by_token(token) if token else None
    if name is None:
        raise AuthError("invalid_device_token")
    return name
