"""SQLite 家庭成员与按人聊天历史库（home-console 计划 U1，R5/R7）。

纪律（计划 U1 Approach）：
  - 与现有设备库同一 db 文件（幂等 CREATE TABLE IF NOT EXISTS，板上老库零迁移，
    无迁移框架）；
  - 成员用代理整数 id（不抄家电库的名字主键）：改名不断历史（消息按 id 外键），
    删除成员级联删除其全部消息（数据最小化，家庭规模无保留需求）；
  - user+assistant 成对落库与裁剪为单事务：任一条失败整体回滚
    （"失败不落库/重试干净"语义的载体）；
  - 每成员仅保留最近 500 条，追加时同事务裁剪；
  - 时间一律 epoch 秒（渲染时设备本地时区，沿 voice_log.py 先例）。

线程模型：单进程 asyncio 单事件循环线程，sqlite3 同步调用即可；
本类不做内部加锁，调用方保证不并发写（沿 appliances/store.py 先例）。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path

__all__ = ["ChatStore", "DuplicateMemberError"]

_DEFAULT_EMOJI = "🙂"
_KEEP_MESSAGES = 500


class DuplicateMemberError(ValueError):
    """成员重名（创建或改名撞上既有名字）时的显式错误。"""


class ChatStore:
    """家庭成员与聊天历史本地库。构造即建表（幂等），close() 关闭连接。

    `now` 可注入（沿 llm/breaker.py 的 clock 先例），默认 time.time；
    db 路径由调用方传入已解析路径，本类不按 cwd 重解析。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：UI 经 uvicorn/TestClient 线程访问；
        # 串行纪律不变（单进程单事件循环线程保证不并发写，见模块 docstring）
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._now = now
        self._init_schema()

    # ---------- 生命周期 ----------

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS members (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL UNIQUE,
                    emoji      TEXT NOT NULL DEFAULT '🙂',
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
                    role       TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content    TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_member_id
                    ON messages(member_id, id);
                """
            )

    def close(self) -> None:
        self._conn.close()

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> "ChatStore":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------- 成员 ----------

    def create_member(self, name: str, emoji: str | None = None) -> dict:
        """创建家庭成员；重名显式拒绝（UNIQUE 约束直接裁决），emoji 缺省落默认头像。"""
        created_at = int(self._now())
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO members (name, emoji, created_at) VALUES (?, ?, ?)",
                    (name, _DEFAULT_EMOJI if emoji is None else emoji, created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateMemberError(f"成员名已存在: {name}") from exc
        return self.get_member(cur.lastrowid)

    def get_member(self, member_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_members(self) -> list[dict]:
        """全部成员，按创建先后（id）排序。"""
        rows = self._conn.execute("SELECT * FROM members ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def rename_member(self, member_id: int, new_name: str) -> dict:
        """改名（消息按 id 外键，历史不断链）；不改 emoji。重名/成员不存在显式报错。

        重名由 UNIQUE 约束裁决（IntegrityError → DuplicateMemberError）；
        成员不存在经 rowcount==0 → KeyError（先于重名：UPDATE 未命中行
        不触发约束，与"先查存在"旧序同语义）。
        """
        try:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE members SET name=? WHERE id=?", (new_name, member_id)
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateMemberError(f"成员名已存在: {new_name}") from exc
        if cur.rowcount == 0:
            raise KeyError(f"成员不存在: {member_id}")
        return self.get_member(member_id)

    def delete_member(self, member_id: int) -> bool:
        """删除成员；其全部消息由 FK ON DELETE CASCADE 级联删除。"""
        with self._conn:
            cur = self._conn.execute("DELETE FROM members WHERE id=?", (member_id,))
        return cur.rowcount > 0

    # ---------- 消息 ----------

    def append_messages(self, member_id: int, rows: Sequence[tuple[str, str]]) -> list[dict]:
        """按序追加消息并在同一事务内裁剪（user+assistant 成对落库的载体）。

        rows 为 (role, content) 序列；任一条插入失败（含 role 非法、成员不存在
        触发 FK）整体回滚，库中不留半批。成功后立即裁剪该成员历史至最近
        500 条。返回实际落库的消息（含 id/created_at）。
        """
        inserted: list[dict] = []
        with self._conn:
            for role, content in rows:
                created_at = int(self._now())
                cur = self._conn.execute(
                    "INSERT INTO messages (member_id, role, content, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (member_id, role, content, created_at),
                )
                inserted.append(
                    {
                        "id": cur.lastrowid,
                        "member_id": member_id,
                        "role": role,
                        "content": content,
                        "created_at": created_at,
                    }
                )
            self._conn.execute(
                """
                DELETE FROM messages
                WHERE member_id=? AND id NOT IN (
                    SELECT id FROM messages WHERE member_id=? ORDER BY id DESC LIMIT ?
                )
                """,
                (member_id, member_id, _KEEP_MESSAGES),
            )
        return inserted

    def recent_messages(self, member_id: int, limit: int = 50) -> list[dict]:
        """该成员最近 limit 条，旧→新（newest-last）排序。"""
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE member_id=? ORDER BY id DESC LIMIT ?",
            (member_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def messages_before(self, member_id: int, before_id: int, limit: int = 50) -> list[dict]:
        """该成员 id 小于 before_id 的更早一页，旧→新排序（"加载更多"翻页用）。"""
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE member_id=? AND id<? ORDER BY id DESC LIMIT ?",
            (member_id, before_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
