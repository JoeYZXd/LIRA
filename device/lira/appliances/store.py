"""SQLite 本地库（U6，R13）：设备/动作/码值/场景/同步元数据的本地持久化。

纪律（计划 U6 Approach）：
  - WAL + `synchronous=FULL`：安全规则库丢失等同安全规则失效，
    写放大可接受（写入低频）；
  - 码值 UPSERT（重学习覆盖同设备+动作）；
  - 删除设备级联删除其码值与场景引用（FK ON DELETE CASCADE，防悬挂引用）；
  - 快照应用 = 单事务原子提交（中途失败整体回滚，设备保持最近完整版本）。

线程模型：单进程 asyncio，sqlite3 同步调用即可（写入低频，毫秒级）；
本类不做内部加锁，调用方（编排器）保证不并发写。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lira.appliances.models import ApplianceModel, SceneModel, SceneStep

__all__ = ["ApplianceStore"]

_META_EPOCH = "snapshot_epoch"
_META_VERSION = "snapshot_version"
_META_DEVICE_TOKEN = "device_token"


class ApplianceStore:
    """本地家电配置库。构造即建表（幂等），上下文管理器负责关闭连接。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：U7 UI 经 uvicorn/TestClient 线程访问口令 meta；
        # 串行纪律不变（单进程编排器保证不并发写，见模块 docstring）
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ---------- 生命周期 ----------

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
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
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        self._conn.close()

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> "ApplianceStore":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------- 家电 / 动作 / 码值 ----------

    def upsert_appliance(self, model: ApplianceModel) -> None:
        """UPSERT 家电及其动作配置。保留既有码值（快照更新不丢学习成果）。"""
        with self._conn:
            self._upsert_appliance_tx(model)

    def _upsert_appliance_tx(self, model: ApplianceModel) -> None:
        """无事务包裹版本（供 apply_snapshot 的外层单事务复用）。"""
        self._conn.execute(
            """
            INSERT INTO appliances (name, aliases, is_high_risk, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                aliases=excluded.aliases,
                is_high_risk=excluded.is_high_risk,
                enabled=excluded.enabled
            """,
            (
                model.name,
                json.dumps(list(model.aliases), ensure_ascii=False),
                int(model.is_high_risk),
                int(model.enabled),
            ),
        )
        for action, triggers in model.actions.items():
            self._conn.execute(
                """
                INSERT INTO appliance_actions (device_name, action, triggers, code)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_name, action) DO UPDATE SET
                    triggers=excluded.triggers
                """,
                (
                    model.name,
                    action,
                    json.dumps(list(triggers), ensure_ascii=False),
                    model.codes.get(action),
                ),
            )
        # 移除配置中已不存在的动作（含其码值），防悬挂
        for action, in self._conn.execute(
            "SELECT action FROM appliance_actions WHERE device_name=?", (model.name,)
        ).fetchall():
            if action not in model.actions:
                self._conn.execute(
                    "DELETE FROM appliance_actions WHERE device_name=? AND action=?",
                    (model.name, action),
                )

    def upsert_code(self, device_name: str, action: str, code: str) -> None:
        """码值 UPSERT：重学习覆盖同设备+动作（R32）。设备不存在则拒绝。"""
        if self.get_appliance(device_name) is None:
            raise KeyError(f"设备不存在: {device_name}")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO appliance_actions (device_name, action, triggers, code)
                VALUES (?, ?, '[]', ?)
                ON CONFLICT(device_name, action) DO UPDATE SET
                    code=excluded.code
                """,
                (device_name, action, code),
            )

    def get_appliance(self, name: str) -> ApplianceModel | None:
        row = self._conn.execute("SELECT * FROM appliances WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        actions: dict[str, tuple[str, ...]] = {}
        codes: dict[str, str] = {}
        for ar in self._conn.execute(
            "SELECT action, triggers, code FROM appliance_actions WHERE device_name=?", (name,)
        ).fetchall():
            actions[ar["action"]] = tuple(json.loads(ar["triggers"]))
            if ar["code"]:
                codes[ar["action"]] = ar["code"]
        return ApplianceModel(
            name=row["name"],
            aliases=tuple(json.loads(row["aliases"])),
            actions=actions,
            codes=codes,
            is_high_risk=bool(row["is_high_risk"]),
            enabled=bool(row["enabled"]),
        )

    def get_all_appliances(self) -> list[ApplianceModel]:
        names = [r["name"] for r in self._conn.execute("SELECT name FROM appliances ORDER BY name")]
        return [a for a in (self.get_appliance(n) for n in names) if a is not None]

    def delete_appliance(self, name: str) -> bool:
        """删除设备；级联删除其码值与场景引用（FK ON DELETE CASCADE）。"""
        with self._conn:
            cur = self._conn.execute("DELETE FROM appliances WHERE name=?", (name,))
            # 场景步骤引用 device_name 无 FK（场景步骤允许先于设备存在性校验），
            # 手动级联清除，防悬挂引用
            self._conn.execute("DELETE FROM scene_steps WHERE device_name=?", (name,))
        return cur.rowcount > 0

    # ---------- 场景 ----------

    def upsert_scene(self, scene: SceneModel) -> None:
        with self._conn:
            self._upsert_scene_tx(scene)

    def _upsert_scene_tx(self, scene: SceneModel) -> None:
        """无事务包裹版本（供 apply_snapshot 的外层单事务复用）。"""
        self._conn.execute(
            "INSERT INTO scenes (name) VALUES (?) ON CONFLICT(name) DO NOTHING",
            (scene.name,),
        )
        self._conn.execute("DELETE FROM scene_steps WHERE scene_name=?", (scene.name,))
        for idx, step in enumerate(scene.steps):
            self._conn.execute(
                "INSERT INTO scene_steps (scene_name, idx, device_name, action) VALUES (?, ?, ?, ?)",
                (scene.name, idx, step.device, step.action),
            )

    def get_scene(self, name: str) -> SceneModel | None:
        row = self._conn.execute("SELECT name FROM scenes WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        steps = tuple(
            SceneStep(device=r["device_name"], action=r["action"])
            for r in self._conn.execute(
                "SELECT device_name, action FROM scene_steps WHERE scene_name=? ORDER BY idx", (name,)
            )
        )
        return SceneModel(name=row["name"], steps=steps)

    def list_scenes(self) -> list[SceneModel]:
        names = [r["name"] for r in self._conn.execute("SELECT name FROM scenes ORDER BY name")]
        return [s for s in (self.get_scene(n) for n in names) if s is not None]

    def delete_scene(self, name: str) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM scenes WHERE name=?", (name,))
        return cur.rowcount > 0

    # ---------- 快照原子应用 ----------

    def apply_snapshot(
        self,
        appliances: list[ApplianceModel],
        scenes: list[SceneModel],
        epoch: int,
        version: int,
        device_token: str | None = None,
    ) -> None:
        """全量快照单事务原子应用：全部替换 + 元数据提交，要么全成要么全回滚。"""
        with self._conn:
            self._conn.execute("DELETE FROM scene_steps")
            self._conn.execute("DELETE FROM scenes")
            self._conn.execute("DELETE FROM appliance_actions")
            self._conn.execute("DELETE FROM appliances")
            for model in appliances:
                self._upsert_appliance_tx(model)
            for scene in scenes:
                self._upsert_scene_tx(scene)
            self._set_meta(_META_EPOCH, str(epoch))
            self._set_meta(_META_VERSION, str(version))
            if device_token is not None:
                self._set_meta(_META_DEVICE_TOKEN, device_token)

    # ---------- 同步元数据 (epoch, version) / token ----------

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._set_meta(key, value)

    def current_epoch(self) -> int | None:
        v = self.get_meta(_META_EPOCH)
        return int(v) if v is not None else None

    def current_version(self) -> int | None:
        v = self.get_meta(_META_VERSION)
        return int(v) if v is not None else None

    def device_token(self) -> str | None:
        return self.get_meta(_META_DEVICE_TOKEN)
