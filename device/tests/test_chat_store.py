"""U1 成员与聊天历史存储测试（计划 U1 Patterns：真库 tmp_path + reopen 验持久化）。

覆盖：成员生命周期（默认 emoji、重名显式拒绝、改名不断链不改头像、级联删除）、
消息追加与单事务语义（坏 role 整批回滚、FK 拒绝孤儿消息）、每成员 500 条裁剪、
before_id 分页、WAL+FULL+FK 连接约定、close/reopen 持久化与幂等建表、注入时钟。
"""

from __future__ import annotations

import sqlite3

import pytest

from lira.chat.store import ChatStore, DuplicateMemberError


def make_store(tmp_path, **kwargs) -> ChatStore:
    return ChatStore(tmp_path / "device.db", **kwargs)


class FakeClock:
    """递增假时钟：created_at 可精确断言（注入 now 沿 llm/breaker.py 的 clock 先例）。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


# ---------- 成员生命周期 ----------


class TestMemberLifecycle:
    def test_create_member_default_emoji_and_injected_clock(self, tmp_path):
        store = make_store(tmp_path, now=FakeClock())
        m = store.create_member("妈妈")
        assert m["id"] == 1
        assert m["name"] == "妈妈"
        assert m["emoji"] == "🙂"  # 默认头像落库
        assert m["created_at"] == 1_700_000_001  # 来自注入时钟（epoch 秒）

    def test_create_member_custom_emoji(self, tmp_path):
        store = make_store(tmp_path)
        m = store.create_member("爸爸", emoji="🧔")
        assert m["emoji"] == "🧔"

    def test_duplicate_name_rejected_explicitly(self, tmp_path):
        store = make_store(tmp_path)
        store.create_member("妈妈")
        with pytest.raises(DuplicateMemberError) as ei:
            store.create_member("妈妈")
        assert isinstance(ei.value, ValueError)
        assert len(store.list_members()) == 1  # 原成员不受影响

    def test_get_member_missing_returns_none(self, tmp_path):
        store = make_store(tmp_path)
        assert store.get_member(999) is None

    def test_list_members_empty_then_ordered_by_id(self, tmp_path):
        store = make_store(tmp_path)
        assert store.list_members() == []
        ids = [store.create_member(n)["id"] for n in ("爷爷", "奶奶", "妈妈")]
        assert [m["name"] for m in store.list_members()] == ["爷爷", "奶奶", "妈妈"]
        assert ids == sorted(ids)

    def test_rename_keeps_history_emoji_and_id(self, tmp_path):
        """改名按 id 关联：历史不断链、emoji 不动、id 不变。"""
        store = make_store(tmp_path)
        m = store.create_member("妈妈", emoji="👩")
        store.append_messages(m["id"], [("user", "早上好"), ("assistant", "早上好呀")])
        renamed = store.rename_member(m["id"], "母亲大人")
        assert renamed["name"] == "母亲大人"
        assert renamed["id"] == m["id"]
        assert renamed["emoji"] == "👩"
        history = store.recent_messages(m["id"])
        assert [r["content"] for r in history] == ["早上好", "早上好呀"]

    def test_rename_to_existing_name_rejected(self, tmp_path):
        store = make_store(tmp_path)
        store.create_member("妈妈")
        b = store.create_member("爸爸")
        with pytest.raises(DuplicateMemberError):
            store.rename_member(b["id"], "妈妈")
        assert store.get_member(b["id"])["name"] == "爸爸"  # 改名失败原名字不变

    def test_rename_missing_member_raises(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(KeyError):
            store.rename_member(999, "谁")

    def test_delete_member_cascades_others_untouched(self, tmp_path):
        """删除成员级联清其全部消息；其他成员消息不受影响。"""
        store = make_store(tmp_path)
        a = store.create_member("妈妈")
        b = store.create_member("爸爸")
        store.append_messages(a["id"], [("user", "妈妈的消息")])
        store.append_messages(b["id"], [("user", "爸爸的消息")])
        assert store.delete_member(a["id"]) is True
        assert store.get_member(a["id"]) is None
        assert store.recent_messages(a["id"]) == []
        assert [r["content"] for r in store.recent_messages(b["id"])] == ["爸爸的消息"]
        assert store.delete_member(a["id"]) is False  # 二次删除幂等返回 False


# ---------- 消息追加 / 查询 / 裁剪 ----------


class TestMessages:
    def test_append_pair_and_recent_order(self, tmp_path):
        """user+assistant 成对追加，历史旧→新、内容与 role 正确、时间递增。"""
        store = make_store(tmp_path, now=FakeClock())
        m = store.create_member("妈妈")
        created = store.append_messages(
            m["id"], [("user", "今天天气怎么样"), ("assistant", "今天晴")]
        )
        assert [r["role"] for r in created] == ["user", "assistant"]
        history = store.recent_messages(m["id"])
        assert [(r["role"], r["content"]) for r in history] == [
            ("user", "今天天气怎么样"),
            ("assistant", "今天晴"),
        ]
        assert history[0]["created_at"] < history[1]["created_at"]

    def test_recent_messages_limit_returns_newest(self, tmp_path):
        store = make_store(tmp_path)
        m = store.create_member("妈妈")
        store.append_messages(m["id"], [("user", f"m{i}") for i in range(5)])
        got = store.recent_messages(m["id"], limit=2)
        assert [r["content"] for r in got] == ["m3", "m4"]  # 最新 N 条，旧→新

    def test_member_without_messages_returns_empty(self, tmp_path):
        store = make_store(tmp_path)
        m = store.create_member("妈妈")
        assert store.recent_messages(m["id"]) == []
        assert store.messages_before(m["id"], before_id=10**9) == []

    def test_messages_before_pagination(self, tmp_path):
        """before_id 分页只取更早一批，旧→新；翻到头返回空。"""
        store = make_store(tmp_path)
        m = store.create_member("妈妈")
        store.append_messages(m["id"], [("user", f"m{i:02d}") for i in range(1, 31)])
        first_page = store.recent_messages(m["id"], limit=10)
        assert [r["content"] for r in first_page] == [f"m{i:02d}" for i in range(21, 31)]
        older = store.messages_before(m["id"], before_id=first_page[0]["id"], limit=10)
        assert [r["content"] for r in older] == [f"m{i:02d}" for i in range(11, 21)]
        oldest = store.messages_before(m["id"], before_id=older[0]["id"], limit=50)
        assert [r["content"] for r in oldest] == [f"m{i:02d}" for i in range(1, 11)]
        assert store.messages_before(m["id"], before_id=oldest[0]["id"]) == []

    def test_trim_keeps_most_recent_500(self, tmp_path):
        """超过 500 条：最旧被裁剪，每成员总量守恒，最新保留。"""
        store = make_store(tmp_path)
        m = store.create_member("妈妈")
        store.append_messages(
            m["id"],
            [("user" if i % 2 == 0 else "assistant", f"m{i:03d}") for i in range(500)],
        )
        store.append_messages(m["id"], [("user", "新消息"), ("assistant", "新回复")])
        history = store.recent_messages(m["id"], limit=10_000)
        assert len(history) == 500  # 总量守恒
        assert history[0]["id"] == 3  # 最旧两条（id 1/2）被裁剪
        assert history[0]["content"] == "m002"
        assert [r["content"] for r in history[-2:]] == ["新消息", "新回复"]

    def test_bad_role_rolls_back_whole_batch(self, tmp_path):
        """单事务语义：批内任一条失败整体回滚，库中不留半批。"""
        store = make_store(tmp_path)
        m = store.create_member("妈妈")
        with pytest.raises(sqlite3.IntegrityError):
            store.append_messages(m["id"], [("user", "ok"), ("system", "bad-role")])
        assert store.recent_messages(m["id"]) == []

    def test_fk_violation_unknown_member(self, tmp_path):
        """FK ON：不存在的成员落消息自然被拒（成员存在性写事务复查属 U2）。"""
        store = make_store(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            store.append_messages(999, [("user", "孤儿消息")])


# ---------- 持久化与连接约定 ----------


class TestPersistence:
    def test_wal_and_full_synchronous_and_fk(self, tmp_path):
        """沿 appliances/store.py 纪律：WAL + synchronous=FULL + FK ON。"""
        store = make_store(tmp_path)
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_close_and_reopen_keeps_members_and_messages(self, tmp_path):
        """close + reopen（WAL 持久化）：成员与消息仍在，emoji/created_at 保真。"""
        path = tmp_path / "device.db"
        store = ChatStore(path, now=FakeClock())
        m = store.create_member("妈妈", emoji="👩")
        store.append_messages(m["id"], [("user", "你好"), ("assistant", "嗨")])
        store.close()

        reopened = ChatStore(path)  # 对含旧表的库二次构造：幂等建表无报错
        got = reopened.get_member(m["id"])
        assert got["name"] == "妈妈" and got["emoji"] == "👩"
        history = reopened.recent_messages(m["id"])
        assert [(r["role"], r["content"]) for r in history] == [
            ("user", "你好"),
            ("assistant", "嗨"),
        ]
        assert history[0]["created_at"] == 1_700_000_002
        reopened.close()

    def test_reopen_then_write_and_constraints_hold(self, tmp_path):
        """重开后写入/裁剪/唯一约束照常（幂等建表不破坏既有数据与约束）。"""
        path = tmp_path / "device.db"
        store = ChatStore(path)
        m = store.create_member("妈妈")
        store.append_messages(m["id"], [("user", "第一批")])
        store.close()

        store2 = ChatStore(path)
        b = store2.create_member("爸爸")
        store2.append_messages(b["id"], [("user", "第二批")])
        assert store2.get_member(m["id"])["name"] == "妈妈"  # 旧成员仍在
        assert [r["content"] for r in store2.recent_messages(b["id"])] == ["第二批"]
        with pytest.raises(DuplicateMemberError):
            store2.create_member("妈妈")  # UNIQUE 约束重开后仍生效
        store2.close()
