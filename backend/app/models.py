"""后台数据模型（U8）：SQLite 持久化 + 配置快照装配。

纪律（与设备端 `lira.appliances.store` 同源对齐，System-Wide Impact
「API surface parity」）：
  - SQLite WAL + `synchronous=FULL`（安全规则库丢失等同安全规则失效）；
  - 数据库文件创建即 `os.chmod(0o600)`（U8 运维面：泄露库文件不泄露
    token——token 只存 sha256 哈希，密码只存 bcrypt 哈希，双重兜底）；
  - 家电/场景表结构与设备端 store 同构（name/aliases/actions/codes/
    is_high_risk/enabled），快照装配经 `app.protocol`（= device/lira/protocol.py
    副本）的 DTO 完成，后台**不可能**产出协议外的字段；
  - 每次配置变更在同一事务内 `version+1`（(epoch, version) 同步键，
    Key Decisions）；epoch 只在重配对流程中换新。

并发模型：单连接 + 进程内 `threading.RLock` 串行化写（写入低频，毫秒级）；
WAL 允许外部连接（如测试/备份）并发读不阻塞写。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app import protocol
from app.protocol import ApplianceDTO, SceneDTO, SceneStepDTO, SnapshotMsg

__all__ = ["Database"]

# settings 白名单与协议 SETTINGS_ALLOWLIST 一致（显式复制，变更须两侧同步）
_SETTING_TYPES: dict[str, type] = dict(protocol.SETTINGS_ALLOWLIST)


class Database:
    """后台库。构造即建表（幂等）+ 首启 epoch 初始化 + 0600 收权。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_schema()
        # 运维面（U8 Approach）：后台 SQLite 文件权限 0600
        for name in (self._path.name, self._path.name + "-wal", self._path.name + "-shm"):
            p = self._path.parent / name
            if p.exists():
                p.chmod(0o600)
        with self._tx() as conn:
            if conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone() is None:
                # epoch 随库而生：库重建/恢复即天然换新（对抗旧快照回放）
                self._set_meta_tx(conn, "epoch", str(secrets.randbits(31) + 1))
                self._set_meta_tx(conn, "version", "0")

    # ---------- 基础设施 ----------

    def _init_schema(self) -> None:
        with self._tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin (
                    id              INTEGER PRIMARY KEY CHECK (id = 1),
                    username        TEXT NOT NULL,
                    password_hash   TEXT NOT NULL,
                    revoked_count   INTEGER NOT NULL DEFAULT 0,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until    REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS devices (
                    name              TEXT PRIMARY KEY,
                    token_hash        TEXT NOT NULL,
                    pending_token_hash TEXT,
                    created_at        REAL NOT NULL,
                    last_seen         REAL NOT NULL DEFAULT 0,
                    ack_epoch         INTEGER,
                    ack_version       INTEGER
                );
                CREATE TABLE IF NOT EXISTS appliances (
                    name         TEXT PRIMARY KEY,
                    aliases      TEXT NOT NULL,
                    is_high_risk INTEGER NOT NULL DEFAULT 0,
                    enabled      INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS appliance_actions (
                    device_name TEXT NOT NULL REFERENCES appliances(name) ON DELETE CASCADE,
                    action      TEXT NOT NULL,
                    triggers    TEXT NOT NULL,
                    code        TEXT,
                    PRIMARY KEY (device_name, action)
                );
                CREATE TABLE IF NOT EXISTS scenes (
                    name TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS scene_steps (
                    scene_name  TEXT NOT NULL REFERENCES scenes(name) ON DELETE CASCADE,
                    idx         INTEGER NOT NULL,
                    device_name TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    PRIMARY KEY (scene_name, idx)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_acks (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    device  TEXT NOT NULL,
                    epoch   INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    applied INTEGER NOT NULL,
                    reason  TEXT,
                    at      REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learn_requests (
                    learn_id   TEXT PRIMARY KEY,
                    device     TEXT NOT NULL,
                    action     TEXT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'pending',
                    code       TEXT,
                    error      TEXT,
                    created_at REAL NOT NULL
                );
                """
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """锁 + 事务：要么全成要么全回滚（快照变更原子性）。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def close(self) -> None:
        self._conn.close()

    def _set_meta_tx(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            self._set_meta_tx(conn, key, value)

    # ---------- (epoch, version) ----------

    def epoch(self) -> int:
        return int(self.get_meta("epoch", "0"))

    def version(self) -> int:
        return int(self.get_meta("version", "0"))

    def _bump_version_tx(self, conn: sqlite3.Connection) -> int:
        new_version = self.version() + 1
        self._set_meta_tx(conn, "version", str(new_version))
        return new_version

    def renew_epoch(self) -> tuple[int, int]:
        """库重建/重配对：epoch 换新、version 归零（对抗性评审结论）。"""
        new_epoch = secrets.randbits(31) + 1
        with self._tx() as conn:
            self._set_meta_tx(conn, "epoch", str(new_epoch))
            self._set_meta_tx(conn, "version", "0")
        return new_epoch, 0

    # ---------- 管理员（首启引导 / 登录限速 / 撤销计数） ----------

    def has_admin(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM admin WHERE id=1").fetchone() is not None

    def create_admin(self, username: str, password_hash: str) -> None:
        """首启设置管理员。已存在则拒绝（系统不存在空/默认密码状态）。"""
        if self.has_admin():
            raise RuntimeError("管理员已存在（首启设置已完成，禁止覆盖）")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO admin (id, username, password_hash) VALUES (1, ?, ?)",
                (username, password_hash),
            )

    def get_admin(self) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM admin WHERE id=1").fetchone()

    def change_password(self, password_hash: str) -> int:
        """改密码：撤销计数 +1 → 全部旧 JWT 会话失效（Key Decisions）。"""
        with self._tx() as conn:
            new_rev = self.get_admin()["revoked_count"] + 1
            conn.execute(
                "UPDATE admin SET password_hash=?, revoked_count=? WHERE id=1",
                (password_hash, new_rev),
            )
        return new_rev

    def record_login_failure(self) -> int:
        """登录失败计数；满 5 次锁 5 分钟（U8 Approach）。返回当前失败数。

        锁定过期即上一轮计数作废（F7）：过期后的首次失败从 1 重新起算——
        否则锁满 5 分钟后一次失败便立即再锁（计数永不复位）。
        """
        with self._tx() as conn:
            admin = self.get_admin()
            now = time.time()
            if admin["locked_until"] and now > admin["locked_until"]:
                attempts = 1
                locked_until = 0
            else:
                attempts = admin["failed_attempts"] + 1
                locked_until = now + 300 if attempts >= 5 else admin["locked_until"]
            conn.execute(
                "UPDATE admin SET failed_attempts=?, locked_until=? WHERE id=1",
                (attempts, locked_until),
            )
        return attempts

    def record_login_success(self) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE admin SET failed_attempts=0, locked_until=0 WHERE id=1"
            )

    # ---------- 设备注册与 token ----------

    @staticmethod
    def hash_token(token: str) -> str:
        """token 只存哈希：泄露库文件不泄露 token（U8 Approach）。"""
        import hashlib

        return hashlib.sha256(token.encode()).hexdigest()

    def create_device(self, name: str) -> str:
        token = self.new_device_token()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO devices (name, token_hash, created_at) VALUES (?, ?, ?)",
                (name, self.hash_token(token), time.time()),
            )
        return token

    @staticmethod
    def new_device_token() -> str:
        import secrets as _secrets

        return "lirad_" + _secrets.token_urlsafe(32)

    def get_device(self, name: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM devices WHERE name=?", (name,)).fetchone()

    def list_devices(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM devices ORDER BY name"))

    def find_device_by_token(self, token: str) -> str | None:
        """按 token 哈希查设备（常量时间比较）。只匹配当前生效 token。"""
        digest = self.hash_token(token)
        with self._lock:
            for row in self._conn.execute(
                "SELECT name, token_hash FROM devices WHERE token_hash IS NOT NULL"
            ):
                if secrets.compare_digest(row["token_hash"], digest):
                    return row["name"]
        return None

    def revoke_device_token(self, name: str) -> str:
        """吊销并重发 token（U8 Approach：可吊销重发）。只返回一次明文。"""
        token = self.new_device_token()
        with self._tx() as conn:
            conn.execute("UPDATE devices SET token_hash=? WHERE name=?",
                         (self.hash_token(token), name))
        return token

    def touch_last_seen(self, name: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE devices SET last_seen=? WHERE name=?", (time.time(), name))

    # ---------- 重配对（epoch 迁移，Key Decisions） ----------

    def start_pairing(self, name: str, pairing_code: str) -> str:
        """子女输入设备屏显配对码：生成新 token（待激活）+ epoch 换新。

        新 token 在设备回执 applied 后激活，旧 token 即刻失效——期间设备
        仍用旧 token 通信（拉到含 pairing_code 的新快照完成迁移）。
        新 token 明文须临时暂存于 meta（`pairing_token:<name>`），因为协议的
        `snapshot.device_token` 字段要求把新 token 下发给设备（设备端仅在
        配对会话内落库）；配对收尾即删除明文，平时库里只有哈希。
        """
        new_token = self.new_device_token()
        with self._tx() as conn:
            device = conn.execute("SELECT * FROM devices WHERE name=?", (name,)).fetchone()
            if device is None:
                raise KeyError(f"设备不存在: {name}")
            conn.execute("UPDATE devices SET pending_token_hash=? WHERE name=?",
                         (self.hash_token(new_token), name))
            self._set_meta_tx(conn, f"pairing:{name}", pairing_code)
            self._set_meta_tx(conn, f"pairing_token:{name}", new_token)
        self.renew_epoch()
        return new_token

    def pairing_code_for(self, name: str) -> str | None:
        code = self.get_meta(f"pairing:{name}")
        return code or None

    def _finalize_pairing(self, name: str, epoch: int, applied: bool) -> None:
        """回执 applied 且 epoch 与当前一致 → 激活新 token、旧 token 失效，
        并清除 pairing 明文暂存。"""
        if not applied or epoch != self.epoch():
            return
        with self._tx() as conn:
            conn.execute(
                "UPDATE devices SET token_hash=pending_token_hash, pending_token_hash=NULL "
                "WHERE name=? AND pending_token_hash IS NOT NULL",
                (name,),
            )
            self._set_meta_tx(conn, f"pairing:{name}", "")
            self._set_meta_tx(conn, f"pairing_token:{name}", "")

    def record_ack(self, name: str, msg: protocol.SnapshotAckMsg) -> None:
        """设备回执入库留痕（U8 Approach）+ 重配对收尾。"""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO snapshot_acks (device, epoch, version, applied, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, msg.epoch, msg.version, int(msg.applied), msg.reason, time.time()),
            )
            conn.execute(
                "UPDATE devices SET ack_epoch=?, ack_version=? WHERE name=?",
                (msg.epoch, msg.version, name),
            )
        self._finalize_pairing(name, msg.epoch, msg.applied)

    def list_acks(self, name: str, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM snapshot_acks WHERE device=? ORDER BY id DESC LIMIT ?",
                (name, limit),
            ))

    # ---------- 家电 / 场景 / 设置（变更即 version+1） ----------

    def upsert_appliance(
        self,
        name: str,
        aliases: tuple[str, ...],
        actions: dict[str, tuple[str, ...]],
        is_high_risk: bool,
        enabled: bool,
    ) -> int:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO appliances (name, aliases, is_high_risk, enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    aliases=excluded.aliases,
                    is_high_risk=excluded.is_high_risk,
                    enabled=excluded.enabled
                """,
                (name, json.dumps(list(aliases), ensure_ascii=False),
                 int(is_high_risk), int(enabled)),
            )
            if actions:
                # 部分更新语义：actions 为空（如开关类局部变更）时保留既有动作
                # 与码值；非空时 UPSERT 覆盖动作/触发词（码值保留，重学习走
                # store_learned_code）。
                for action, triggers in actions.items():
                    conn.execute(
                        """
                        INSERT INTO appliance_actions (device_name, action, triggers, code)
                        VALUES (?, ?, ?, NULL)
                        ON CONFLICT(device_name, action) DO UPDATE SET
                            triggers=excluded.triggers
                        """,
                        (name, action, json.dumps(list(triggers), ensure_ascii=False)),
                    )
            return self._bump_version_tx(conn)

    def delete_appliance(self, name: str) -> int:
        with self._tx() as conn:
            conn.execute("DELETE FROM appliances WHERE name=?", (name,))
            conn.execute("DELETE FROM scene_steps WHERE device_name=?", (name,))
            return self._bump_version_tx(conn)

    def get_appliance(self, name: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM appliances WHERE name=?", (name,)
            ).fetchone()

    def list_appliances(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM appliances ORDER BY name"))

    def appliance_actions(self, name: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM appliance_actions WHERE device_name=? ORDER BY action", (name,)
            ))

    def store_learned_code(self, device_name: str, action: str, code: str) -> int:
        """学习码值入库（R32/AE5）：UPSERT 覆盖 + version+1。"""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO appliance_actions (device_name, action, triggers, code)
                VALUES (?, ?, '[]', ?)
                ON CONFLICT(device_name, action) DO UPDATE SET code=excluded.code
                """,
                (device_name, action, code),
            )
            return self._bump_version_tx(conn)

    def upsert_scene(self, name: str, steps: list[tuple[str, str]]) -> int:
        with self._tx() as conn:
            conn.execute("INSERT INTO scenes (name) VALUES (?) "
                         "ON CONFLICT(name) DO NOTHING", (name,))
            conn.execute("DELETE FROM scene_steps WHERE scene_name=?", (name,))
            for idx, (device, action) in enumerate(steps):
                conn.execute(
                    "INSERT INTO scene_steps (scene_name, idx, device_name, action) "
                    "VALUES (?, ?, ?, ?)",
                    (name, idx, device, action),
                )
            return self._bump_version_tx(conn)

    def delete_scene(self, name: str) -> int:
        with self._tx() as conn:
            conn.execute("DELETE FROM scenes WHERE name=?", (name,))
            return self._bump_version_tx(conn)

    def list_scenes(self) -> list[dict[str, Any]]:
        with self._lock:
            scenes = []
            for row in self._conn.execute("SELECT name FROM scenes ORDER BY name"):
                steps = list(self._conn.execute(
                    "SELECT device_name, action FROM scene_steps WHERE scene_name=? ORDER BY idx",
                    (row["name"],),
                ))
                scenes.append({"name": row["name"],
                               "steps": [(s["device_name"], s["action"]) for s in steps]})
            return scenes

    def set_setting(self, key: str, value: Any) -> int:
        """设置项写入（键必须在协议白名单内，类型严格）。"""
        expected = _SETTING_TYPES.get(key)
        if expected is None:
            raise ValueError(f"未知设置键: {key!r}（白名单外拒绝）")
        if expected is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"settings.{key} 应为数字")
            value = float(value)
        elif not isinstance(value, expected):
            raise ValueError(f"settings.{key} 类型错误")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            return self._bump_version_tx(conn)

    def settings_all(self) -> dict[str, Any]:
        """读取全部设置（键/类型已由写入侧白名单保证）。"""
        with self._lock:
            return {
                row["key"]: json.loads(row["value"])
                for row in self._conn.execute("SELECT key, value FROM settings")
            }

    # ---------- 学习请求（R32） ----------

    def create_learn(self, learn_id: str, device: str, action: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO learn_requests (learn_id, device, action, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (learn_id, device, action, time.time()),
            )

    def claim_pending_learns(self) -> list[sqlite3.Row]:
        """WS 循环领取待下发学习请求（pending → sent），返回领取的行。

        Phase 1 单设备：任意已连接硬件设备领取全部 pending 请求
        （`device` 字段语义为家电名，与协议 LearnStartMsg.device 一致）。
        """
        with self._tx() as conn:
            rows = list(conn.execute(
                "SELECT * FROM learn_requests WHERE status='pending' ORDER BY created_at",
            ))
            for row in rows:
                conn.execute("UPDATE learn_requests SET status='sent' WHERE learn_id=?",
                             (row["learn_id"],))
            return rows

    def finish_learn(self, learn_id: str, code: str | None, error: str | None) -> sqlite3.Row | None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE learn_requests SET status='done', code=?, error=? WHERE learn_id=?",
                (code, error, learn_id),
            )
        return self.get_learn(learn_id)

    def get_learn(self, learn_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM learn_requests WHERE learn_id=?", (learn_id,)
            ).fetchone()

    # ---------- 快照装配（经协议 DTO，字段即协议） ----------

    def build_snapshot(self, device_name: str | None = None) -> SnapshotMsg:
        """装配全量配置快照；装配后过 `validate_snapshot`（后台不自产非法快照）。

        重配对会话中的设备会额外收到 pairing_code + device_token（新 token），
        仅该设备可见——协议纪律：这两字段只在重配对会话内出现并被设备接受。
        """
        with self._lock:
            appliances: list[ApplianceDTO] = []
            for row in self.list_appliances():
                actions: dict[str, tuple[str, ...]] = {}
                for ar in self.appliance_actions(row["name"]):
                    actions[ar["action"]] = tuple(json.loads(ar["triggers"]))
                appliances.append(ApplianceDTO(
                    name=row["name"],
                    aliases=tuple(json.loads(row["aliases"])),
                    actions=actions,
                    is_high_risk=bool(row["is_high_risk"]),
                    enabled=bool(row["enabled"]),
                ))
            scenes = tuple(
                SceneDTO(name=s["name"],
                         steps=tuple(SceneStepDTO(device=d, action=a) for d, a in s["steps"]))
                for s in self.list_scenes()
            )
        snapshot = SnapshotMsg(
            epoch=self.epoch(),
            version=self.version(),
            appliances=tuple(appliances),
            scenes=scenes,
            settings=self.settings_all(),
        )
        if device_name is not None:
            pairing_code = self.pairing_code_for(device_name)
            if pairing_code:
                device = self.get_device(device_name)
                if device is not None and device["pending_token_hash"] is not None:
                    # pending token 只有哈希在库——此处需要明文才能下发。
                    # 因此 start_pairing 把明文暂存于 pairing meta（见下）。
                    pending_plain = self.get_meta(f"pairing_token:{device_name}")
                    if pending_plain:
                        object.__setattr__(snapshot, "pairing_code", pairing_code)
                        object.__setattr__(snapshot, "device_token", pending_plain)
        protocol.validate_snapshot(snapshot)
        return snapshot
