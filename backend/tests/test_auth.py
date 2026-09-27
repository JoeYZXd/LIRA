"""U8 鉴权面测试（计划 Test scenarios 第 2 条 + 补充缺口：CSRF、改密失效、
首启强制设置、token 哈希、吊销重发、0600 权限）。

覆盖：管理员首启引导（无空/默认密码状态）、登录限速（5 次锁 5 分钟）、
JWT 撤销计数（改密码全体会话失效）、CSRF 强制、设备 token 哈希存储与
`X-Device-Token` 401 通道。
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from urllib.parse import unquote

import pytest

from app import auth
from app.auth import AuthError
from conftest import apost, extract_csrf


# ---------- 首启引导（R14：无空/默认密码状态） ----------

def test_first_boot_forces_setup_before_any_page(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"


def test_setup_rejects_weak_credentials_then_succeeds(client):
    r = client.post("/setup", data={"username": "p", "password": "short"})
    assert r.status_code == 400  # 弱凭据拒绝（不存在默认密码状态）
    assert not client.app.state.db.has_admin()
    r = client.post("/setup", data={"username": "parent", "password": "password123"},
                    follow_redirects=False)
    assert r.status_code == 303
    # 登录态 cookie 就位
    assert client.cookies[auth.SESSION_COOKIE]
    assert client.cookies[auth.CSRF_COOKIE]
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_setup_refuses_after_admin_exists(admin):
    db = admin.app.state.db
    r = admin.post("/setup", data={"username": "attacker", "password": "attackerpass"},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert db.get_admin()["username"] == "parent"


# ---------- 登录限速（5 次锁 5 分钟） ----------

def test_login_rate_limit_locks_after_five_failures(admin):
    for i in range(5):
        r = admin.post("/login", data={"username": "parent", "password": "wrong"},
                       follow_redirects=False)
        assert r.status_code == 303
    remaining = auth.login_locked(admin.app.state.db)
    assert remaining > 0, "5 次失败后应进入锁定"
    # 正确凭据在锁定期内同样被拒
    r = admin.post("/login", data={"username": "parent", "password": "password123"},
                   follow_redirects=False)
    assert r.headers["location"].startswith("/login?error=")
    assert "锁定" in unquote(r.headers["location"])


def test_login_lock_expiry_resets_failure_counter(admin):
    """F7：锁定过期即计数作废——过期后 1 次失败不得立即再锁，须重新凑满 5 次。"""
    db = admin.app.state.db
    for _ in range(5):
        assert not auth.verify_login(db, "parent", "wrong")
    assert auth.login_locked(db) > 0
    # 时钟拨过锁定期（locked_until 置为过去）
    conn = sqlite3.connect(db._path)
    conn.execute("UPDATE admin SET locked_until=? WHERE id=1", (time.time() - 1,))
    conn.commit()
    conn.close()
    assert auth.login_locked(db) == 0.0
    # 过期后首次失败：新一轮计数第 1 次，不再立即锁定
    assert not auth.verify_login(db, "parent", "wrong")
    assert db.get_admin()["failed_attempts"] == 1
    assert auth.login_locked(db) == 0.0
    # 仍须凑满 5 次才重新锁定
    for _ in range(3):
        assert not auth.verify_login(db, "parent", "wrong")
    assert auth.login_locked(db) == 0.0  # 第 4 次，尚未锁定
    assert not auth.verify_login(db, "parent", "wrong")  # 第 5 次 → 重新锁定
    assert auth.login_locked(db) > 0


# ---------- SEC-1：家电详情页鉴权（与会话纪律一致） ----------

def test_appliance_page_requires_login(app, admin):
    """SEC-1：未登录访问 /appliances/{name} 必须 303 到 /login，已登录 200。"""
    from fastapi.testclient import TestClient

    r = apost(admin, "/admin/appliances", name="取暖器", aliases="取暖器",
              action="打开", triggers="打开取暖器")
    assert r.status_code == 303
    # 未登录（无会话 cookie 的全新客户端）→ 重定向登录
    with TestClient(app) as fresh:
        r = fresh.get("/appliances/取暖器", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    # 已登录 → 200
    r = admin.get("/appliances/取暖器", follow_redirects=False)
    assert r.status_code == 200


# ---------- 会话与撤销计数 ----------

def test_session_cookie_flags(admin):
    assert admin.cookies[auth.SESSION_COOKIE]
    # TestClient cookie jar 保留属性（HttpOnly + SameSite=Strict）
    session_cookie = next(c for c in admin.cookies.jar
                          if c.name == auth.SESSION_COOKIE)
    assert session_cookie.has_nonstandard_attr("HttpOnly")
    assert session_cookie.get_nonstandard_attr("SameSite").lower() == "strict"


def test_password_change_revokes_all_sessions(admin):
    old_cookie = admin.cookies[auth.SESSION_COOKIE]
    r = apost(admin, "/admin/password", old_password="password123",
              new_password="newpassword456", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # 旧 cookie 即便原样重放也失效（撤销计数不匹配）
    admin.cookies.set(auth.SESSION_COOKIE, old_cookie)
    r = admin.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # 新密码可登录
    r = admin.post("/login", data={"username": "parent", "password": "newpassword456"},
                   follow_redirects=False)
    assert r.headers["location"] == "/"


def test_csrf_required_for_admin_writes(admin):
    r = admin.post("/admin/settings/privacy", data={"value": "on"},
                   follow_redirects=False)
    assert r.status_code == 403, "缺 CSRF 的管理端写入必须被拒绝"
    assert admin.app.state.db.settings_all().get("privacy_mode") is None
    # 回传错值同样拒绝
    r = admin.post("/admin/settings/privacy", data={"value": "on", "_csrf": "forged"},
                   follow_redirects=False)
    assert r.status_code == 403


def test_tampered_session_rejected(admin):
    cookie = admin.cookies[auth.SESSION_COOKIE]
    admin.cookies.set(auth.SESSION_COOKIE, cookie[:-3] + "aaa")
    r = admin.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------- 密码哈希（bcrypt 主路径 + scrypt 兜底可验证） ----------

def test_password_hashing_roundtrip():
    stored = auth.hash_password("s3cret-pass")
    assert stored != "s3cret-pass" and "$" in stored
    assert auth.verify_password("s3cret-pass", stored)
    assert not auth.verify_password("wrong", stored)
    # scrypt 兜底格式可被 verify 路由正确处理（bcrypt 缺失环境等效）
    import hashlib, secrets
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(b"s3cret-pass", salt=salt, n=2**14, r=8, p=1)
    scrypt_stored = f"scrypt${salt.hex()}${digest.hex()}"
    assert auth.verify_password("s3cret-pass", scrypt_stored)
    assert not auth.verify_password("wrong", scrypt_stored)


# ---------- 设备 token：哈希存储 + X-Device-Token 401 通道 ----------

def test_device_token_stored_hashed_and_revocable(admin, device_token):
    db = admin.app.state.db
    row = db.get_device("客厅设备")
    assert row["token_hash"] != device_token
    assert len(row["token_hash"]) == 64  # sha256 hex
    assert db.find_device_by_token(device_token) == "客厅设备"
    assert db.find_device_by_token("forged-token") is None
    # 吊销重发：旧 token 即刻失效，新 token 只返回一次
    new_token = db.revoke_device_token("客厅设备")
    assert new_token != device_token
    assert db.find_device_by_token(device_token) is None
    assert db.find_device_by_token(new_token) == "客厅设备"


def test_device_api_requires_token(client, admin, device_token):
    # 无 token → 401
    r = client.get("/api/device/snapshot")
    assert r.status_code == 401
    # 错 token → 401
    r = client.get("/api/device/snapshot", headers={"X-Device-Token": "wrong-token"})
    assert r.status_code == 401
    # 正确 token → 200
    r = client.get("/api/device/snapshot", headers={"X-Device-Token": device_token})
    assert r.status_code == 200


# ---------- 运维面：库文件 0600 ----------

def test_db_file_permission_0600(tmp_path):
    from app.main import create_app

    db_path = tmp_path / "perm.db"
    create_app(str(db_path))
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    assert mode == 0o600, f"后台库文件应为 0600，实际 {oct(mode)}"


def test_scrypt_or_bcrypt_in_use():
    """bcrypt 已装则用 bcrypt；否则 scrypt 兜底——两者都满足『不可逆存储』。"""
    stored = auth.hash_password("x")
    assert stored.startswith("$2") or stored.startswith("scrypt$")
