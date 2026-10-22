"""U6 多候选声纹积累测试（KTD5）：固定向量 + 可控时钟，store 与提取器无关。

覆盖（Execution note：先用固定向量/可控时钟证明，再接真实提取器）：
  - AE2：A/B/A 证据归到稳定候选；重复/短/低能量/削波/歧义不增加进度；
  - 进度数学：min(段数比, 时长比)、一致性未过封顶 0.99、达标才 ready 1.0；
  - 重启一致、旧库幂等打开、策略/模型变化明确失效（不静默增长）；
  - AE5：隐私开启零写入（observe 内第二道防线；主流程复检在 test_main_loop）；
  - 上限/过期/去重滚动可预期；候选不产生个人记忆权限（KTD6）。
"""

from __future__ import annotations

import math
import sqlite3

import numpy as np
import pytest

from lira.config import SpeakerConfig
from lira.personal_memory import PersonalMemory
from lira.speaker_candidates import (
    ASSOCIATION_PENDING,
    MAX_DEDUP_KEYS,
    CandidateConflict,
    CandidateNotFound,
    VoiceCandidates,
)

SAMPLE_RATE = 16000


def vec(deg: float) -> np.ndarray:
    """固定单位向量：与 x 轴夹角 deg 度（2 维，便于精确控制余弦）。"""
    rad = math.radians(deg)
    return np.array([math.cos(rad), math.sin(rad)], np.float32)


A = vec(0.0)  # 说话人 A 的固定向量
# 说话人 B：cos(A,B)≈0.64 < assign_threshold(0.73)，两候选互不吸附；
# 又与各自的角平分线 MIX 保持 cos≈0.906 ≥ match_threshold(0.80)——
# 混合向量对两个候选边际为 0，用于歧义拒绝场景。
B = vec(50.0)
MIX = ((A + B) / np.linalg.norm(A + B)).astype(np.float32)


def samples_for(seconds: float, *, level: float = 0.2, seed: int = 0) -> np.ndarray:
    """可控假音频：指定时长的带噪段（内容与向量无关——store 只看向量）。"""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(SAMPLE_RATE * seconds)) * level).astype(np.float32)


class Clock:
    """可控时钟（快进验证 ttl）。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_store(tmp_path, *, fingerprint="fp-a:2", cfg=None, profiles=lambda: [],
               privacy=lambda: False, clock=None):
    return open_store(tmp_path, fingerprint=fingerprint, cfg=cfg, profiles=profiles,
                      privacy=privacy, clock=clock)


def make_memory_store(tmp_path, *, fingerprint="fp-a:2", cfg=None,
                      privacy=lambda: False, clock=None):
    """共享连接形态（U7 生产装配）：PersonalMemory + 候选存储同一 SQLite 连接。

    members/voice_candidates 晋升同事务只有在这种形态下成立；用例结束直接
    people.close()（无后台任务，无需走 async close）。
    """
    memory = PersonalMemory(tmp_path / "device.db", None, "kimi-k2.6", privacy)
    store = VoiceCandidates(memory.db, fingerprint=fingerprint,
                            profiles_provider=memory.profiles,
                            params=cfg or SpeakerConfig(), privacy=privacy,
                            now=clock or Clock())
    return store, memory


def raw_member_factory(memory):
    """promote_new_name 的成员创建适配（同 personal_memory.SpeakerIdentity
    的内联插入：不自带 commit，事务归 promote 的外层 with 管）。"""
    def create(name):
        cur = memory.people._conn.execute(
            "INSERT INTO members (name, emoji, created_at) VALUES (?, '🙂', ?)", (name, 0))
        return cur.lastrowid
    return create


def open_store(tmp_path, *, fingerprint="fp-a:2", cfg=None, profiles=lambda: [],
               privacy=lambda: False, clock=None):
    """（重）打开候选存储——同一 db 文件重开即模拟重启。"""
    db = sqlite3.connect(str(tmp_path / "device.db"))
    db.row_factory = sqlite3.Row
    store = VoiceCandidates(db, fingerprint=fingerprint, profiles_provider=profiles,
                            params=cfg or SpeakerConfig(), privacy=privacy,
                            now=clock or Clock())
    return store, db, clock or Clock()


def candidate_rows(store):
    return {c["candidate_id"]: c for c in store.snapshot()["candidates"]}


def table_counts(store):
    return tuple(
        store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("voice_candidates", "voice_candidate_evidence")
    )


# ---------- AE2：A/B/A 隔离与质量门 ----------


def test_aba_alternation_lands_on_two_stable_candidates(tmp_path):
    store, _, _ = make_store(tmp_path)
    a_samples = samples_for(4.0, seed=1)
    v1 = store.observe(A, a_samples)
    v2 = store.observe(B, samples_for(4.0, seed=2))
    v3 = store.observe(A, samples_for(4.0, seed=3))
    assert (v1.outcome, v2.outcome, v3.outcome) == ("accepted", "accepted", "accepted")
    assert v3.candidate_id == v1.candidate_id, "A 的证据必须归回 A 的候选"
    assert v2.candidate_id != v1.candidate_id
    active = [c for c in store.snapshot()["candidates"]
              if c["status"] in ("accumulating", "ready")]
    assert len(active) == 2, "A/B/A 交替后应恰有两个稳定候选"
    counts = {c["candidate_id"]: c["segment_count"] for c in active}
    assert counts == {v1.candidate_id: 2, v2.candidate_id: 1}


def test_replayed_samples_are_duplicates_and_do_not_grow(tmp_path):
    store, _, _ = make_store(tmp_path)
    a_samples = samples_for(4.0, seed=1)
    first = store.observe(A, a_samples)
    assert first.outcome == "accepted"
    dup = store.observe(A, a_samples)
    assert dup.outcome == "duplicate"
    assert candidate_rows(store)[first.candidate_id]["segment_count"] == 1


def test_quality_gates_reject_without_progress(tmp_path):
    store, _, _ = make_store(tmp_path)
    ok = store.observe(A, samples_for(4.0, seed=1))
    assert ok.outcome == "accepted"
    short = store.observe(A, samples_for(0.3, seed=2))
    quiet = store.observe(A, samples_for(4.0, seed=3) * 0.001)
    loud = samples_for(4.0, seed=4)
    loud[: int(SAMPLE_RATE * 0.5)] = 1.0  # 12.5% 满幅削波 > 上限 5%
    clipped = store.observe(A, loud)
    assert short.outcome == "too_short"
    assert quiet.outcome == "low_energy"
    assert clipped.outcome == "clipped"
    row = candidate_rows(store)[ok.candidate_id]
    assert row["segment_count"] == 1
    assert row["total_seconds"] == pytest.approx(4.0)


def test_mixture_of_two_candidates_is_ambiguous(tmp_path):
    store, _, _ = make_store(tmp_path)
    store.observe(A, samples_for(4.0, seed=1))
    store.observe(B, samples_for(4.0, seed=2))
    mixed = store.observe(MIX, samples_for(4.0, seed=3))
    assert mixed.outcome == "ambiguous", "两人混合的声音归属不明确，不得计入"
    assert all(c["segment_count"] == 1 for c in candidate_rows(store).values())


# ---------- 进度数学与就绪条件 ----------


def test_progress_is_min_of_segment_and_seconds_ratio(tmp_path):
    store, _, _ = make_store(tmp_path)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    assert v1.progress == pytest.approx(1.0 / 3.0, abs=1e-3)  # min(1/3, 0.4)
    v2 = store.observe(A, samples_for(4.0, seed=2))
    assert v2.progress == pytest.approx(2.0 / 3.0, abs=1e-3)  # min(2/3, 0.8)


def test_targets_met_but_inconsistent_caps_at_99(tmp_path):
    """三段两两夹角最大 40°：每段都过与聚合向量的一致性门，但两两最小
    余弦 cos40°≈0.766 < 0.85 → 不 ready，进度封顶 99%。"""
    store, _, _ = make_store(tmp_path)
    assert store.observe(vec(0.0), samples_for(4.0, seed=1)).outcome == "accepted"
    assert store.observe(vec(-20.0), samples_for(4.0, seed=2)).outcome == "accepted"
    v3 = store.observe(vec(20.0), samples_for(4.0, seed=3))
    assert v3.outcome == "accepted"  # 与当时聚合向量 cos30°≈0.866 ≥ 0.85
    assert v3.progress == pytest.approx(0.99)
    row = candidate_rows(store)[v3.candidate_id]
    assert row["status"] == "accumulating"
    assert row["consistency_passed"] is False


def test_ready_when_count_seconds_and_consistency_all_met(tmp_path):
    store, _, _ = make_store(tmp_path)
    store.observe(A, samples_for(4.0, seed=1))
    store.observe(A, samples_for(4.0, seed=2))
    v3 = store.observe(A, samples_for(4.0, seed=3))
    assert v3.outcome == "accepted" and v3.progress == 1.0
    row = candidate_rows(store)[v3.candidate_id]
    assert row["status"] == "ready" and row["consistency_passed"] is True
    assert store.snapshot()["summary"]["ready_count"] == 1
    assert store.snapshot()["summary"]["accumulating_count"] == 0


def test_snapshot_counts_reinterpret_saved_evidence(tmp_path):
    """Verification：进度可由保存证据重新解释（count/seconds 与写入次数吻合）。"""
    store, _, _ = make_store(tmp_path)
    store.observe(A, samples_for(4.0, seed=1))
    store.observe(A, samples_for(4.0, seed=2))
    rows = store.db.execute(
        "SELECT duration FROM voice_candidate_evidence ORDER BY id").fetchall()
    row = next(iter(candidate_rows(store).values()))
    assert row["segment_count"] == len(rows) == 2
    assert row["total_seconds"] == pytest.approx(sum(r[0] for r in rows))
    assert row["targets"] == {"segments": 3, "seconds": 10.0}


# ---------- 重启一致与旧库幂等 ----------


def test_restart_with_same_evidence_and_strategy_preserves_progress(tmp_path):
    store, db, clock = make_store(tmp_path)
    store.observe(A, samples_for(4.0, seed=1))
    store.observe(A, samples_for(4.0, seed=2))
    before = store.snapshot()
    db.close()
    store2, db2, _ = open_store(tmp_path, clock=clock)
    assert store2.snapshot() == before
    # 去重跨重启稳定：同片段重放仍被现存标识拦截
    assert store2.observe(A, samples_for(4.0, seed=1)).outcome == "duplicate"
    db2.close()


async def test_idempotent_open_keeps_legacy_tables_and_adds_no_member_permission(tmp_path):
    """旧库幂等打开：既有 members/voice_profiles/memory_turns 原样；候选表
    与 members 相互独立（无 member_id 列 → 候选不产生个人记忆权限，KTD6）。"""
    path = tmp_path / "device.db"
    legacy = sqlite3.connect(str(path))
    legacy.executescript('''
        CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '🙂');
        CREATE TABLE voice_profiles (
            member_id INTEGER PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
            vector BLOB NOT NULL);
        CREATE TABLE memory_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            user TEXT NOT NULL, assistant TEXT NOT NULL, topic TEXT NOT NULL,
            day TEXT NOT NULL, created_at REAL NOT NULL);
    ''')
    legacy.execute("INSERT INTO members(name) VALUES ('妈妈')")
    legacy.execute("INSERT INTO voice_profiles VALUES (1, ?)",
                   (A.astype(np.float32).tobytes(),))
    legacy.execute("INSERT INTO memory_turns(member_id,user,assistant,topic,day,created_at)"
                   " VALUES (1,'查天气','今天有雨','weather_search','2026-10-14',0)")
    legacy.commit()
    legacy.close()

    memory = PersonalMemory(path, None, "kimi-k2.6", lambda: False)
    try:
        # 生产形态：候选与 PersonalMemory 同一 SQLite 连接（main 装配传
        # personal_memory.db），构造幂等建表不触碰上方预填的旧表
        store = VoiceCandidates(memory.db, fingerprint="fp-a:2",
                                profiles_provider=memory.profiles,
                                params=SpeakerConfig(), privacy=lambda: False,
                                now=Clock())
        verdict = store.observe(B, samples_for(4.0, seed=1))  # B 与妈妈声纹 cos≈0.64
        assert verdict.outcome == "accepted"
        profiles = memory.profiles()
        assert [(mid, np.allclose(v, A)) for mid, v in profiles] == [(1, True)], "既有声纹不变"
        assert memory.db.execute("SELECT COUNT(*) FROM memory_turns").fetchone()[0] == 1
        columns = {r[1] for r in memory.db.execute("PRAGMA table_info(voice_candidates)")}
        # U7：三列已加（分支内直接改 CREATE TABLE + ALTER 兜底），但 KTD6 的
        # 物理边界改由"确认前恒 NULL"承载：列存在、值全空、无到 members 的外键
        assert {"pending_name", "proposed_member_id", "member_id"} <= columns
        assert all(r[0] is None
                   for r in memory.db.execute("SELECT member_id FROM voice_candidates"))
        fks = list(memory.db.execute("PRAGMA foreign_key_list(voice_candidates)"))
        assert all(fk[2] != "members" for fk in fks), "候选表不建到 members 的外键"
    finally:
        await memory.close()


# ---------- 策略 / 模型变化 ----------


def test_strategy_version_change_invalidates_without_silent_growth(tmp_path):
    store, db, clock = make_store(tmp_path)
    first = store.observe(A, samples_for(4.0, seed=1))
    old_id = first.candidate_id
    db.close()
    store2, db2, _ = open_store(tmp_path, cfg=SpeakerConfig(strategy_version=2), clock=clock)
    row = candidate_rows(store2)[old_id]
    assert row["status"] == "stale", "策略变化必须明确失效，不能静默续用旧证据"
    fresh = store2.observe(A, samples_for(4.0, seed=2))
    assert fresh.outcome == "accepted" and fresh.candidate_id != old_id
    assert fresh.progress <= 0.99, "新策略下从零积累，不继承旧进度"
    db2.close()


def test_fingerprint_change_marks_stale_and_starts_from_zero(tmp_path):
    store, db, clock = make_store(tmp_path)
    first = store.observe(A, samples_for(4.0, seed=1))
    db.close()
    store2, db2, _ = open_store(tmp_path, fingerprint="fp-b:2", clock=clock)
    assert candidate_rows(store2)[first.candidate_id]["status"] == "stale"
    fresh = store2.observe(A, samples_for(4.0, seed=2))
    assert fresh.outcome == "accepted" and fresh.candidate_id != first.candidate_id
    assert fresh.progress == pytest.approx(1.0 / 3.0, abs=1e-3)
    db2.close()


def test_dimension_mismatch_is_rejected_as_stale_fingerprint(tmp_path):
    """指纹声称 3 维而来的是 2 维向量：指纹未反映的模型变化，拒绝且零写入。"""
    store, _, _ = make_store(tmp_path, fingerprint="fp-a:3")
    verdict = store.observe(A, samples_for(4.0, seed=1))
    assert verdict.outcome == "stale_fingerprint"
    assert table_counts(store) == (0, 0)


# ---------- 已建立成员（F1：现有识别不受影响） ----------


def test_established_profile_creates_no_candidate(tmp_path):
    store, _, _ = make_store(tmp_path, profiles=lambda: [(7, A)])
    verdict = store.observe(A, samples_for(4.0, seed=1))
    # U8 COR-3：established 的 id 是 member_id——不进 candidate_id 字段
    #（防遥测文本把成员 id 误记成候选 id），member id 走 reason 文案
    assert verdict.outcome == "established" and verdict.candidate_id is None
    assert "member=7" in verdict.reason
    assert table_counts(store) == (0, 0), "已建立成员不建候选"
    stranger = store.observe(B, samples_for(4.0, seed=2))
    assert stranger.outcome == "accepted", "未命中的声音仍正常积累候选"


def test_two_similar_profiles_are_ambiguous(tmp_path):
    store, _, _ = make_store(tmp_path, profiles=lambda: [(1, A), (2, B)])
    verdict = store.observe(MIX, samples_for(4.0, seed=1))
    assert verdict.outcome == "ambiguous", "两个成员边际不足：声音归属不明确"
    assert table_counts(store) == (0, 0)


# ---------- 上限、过期与去重滚动 ----------


def test_bound_reached_when_candidate_slots_full(tmp_path):
    store, _, _ = make_store(tmp_path, cfg=SpeakerConfig(max_candidates=1))
    first = store.observe(A, samples_for(4.0, seed=1))
    second = store.observe(B, samples_for(4.0, seed=2))
    assert first.outcome == "accepted"
    assert second.outcome == "bound_reached"
    assert table_counts(store)[0] == 1


def test_ttl_expiry_is_lazy_and_predictable(tmp_path):
    clock = Clock(1000.0)
    store, _, _ = make_store(tmp_path, clock=clock)
    first = store.observe(A, samples_for(4.0, seed=1))
    clock.t += 6 * 86400
    assert candidate_rows(store)[first.candidate_id]["status"] == "accumulating"
    clock.t += 2 * 86400  # 累计 8 天 > ttl 7 天
    row = candidate_rows(store)[first.candidate_id]
    assert row["status"] == "expired"


def test_dedup_keys_roll_at_global_limit(tmp_path):
    store, _, _ = make_store(tmp_path)
    for i in range(MAX_DEDUP_KEYS + 5):
        verdict = store.observe(A, samples_for(1.2, seed=i))
        assert verdict.outcome == "accepted"
    kept = store.db.execute("SELECT COUNT(*) FROM voice_candidate_evidence").fetchone()[0]
    assert kept == MAX_DEDUP_KEYS, "去重标识全局滚动保留最近 100 条"
    row = next(iter(candidate_rows(store).values()))
    assert row["segment_count"] == MAX_DEDUP_KEYS + 5, "候选累计计数不因标识滚动回退"


# ---------- 隐私联锁（AE5 第二道防线） ----------


def test_privacy_blocks_all_writes(tmp_path):
    store, _, _ = make_store(tmp_path, privacy=lambda: True)
    verdict = store.observe(A, samples_for(4.0, seed=1))
    assert verdict.outcome == "privacy"
    assert table_counts(store) == (0, 0)


def test_late_result_after_privacy_flip_is_rejected(tmp_path):
    """隐私在提取期间开启：晚到的提取结果在 observe 内被拒（第二道防线）。"""
    enabled = [False]
    store, _, _ = make_store(tmp_path, privacy=lambda: enabled[0])
    first = store.observe(A, samples_for(4.0, seed=1))
    assert first.outcome == "accepted"
    enabled[0] = True  # 主流程复检（第一道）之后隐私才翻转 → 晚到结果
    late = store.observe(A, samples_for(4.0, seed=2))
    assert late.outcome == "privacy"
    assert candidate_rows(store)[first.candidate_id]["segment_count"] == 1


# ---------- 快照汇总（U7 磁贴数据面） ----------


def test_snapshot_summary_and_candidate_wording(tmp_path):
    profiles = []
    store, _, _ = make_store(tmp_path, profiles=lambda: profiles)
    store.observe(A, samples_for(4.0, seed=1))
    store.observe(B, samples_for(4.0, seed=2))
    summary = store.snapshot()["summary"]
    assert summary == {"established_count": 0, "accumulating_count": 2,
                       "ready_count": 0, "awaiting_console_count": 0}
    profiles.append((1, A))
    assert store.snapshot()["summary"]["established_count"] == 1
    entry = store.snapshot()["candidates"][0]
    assert "candidate_id" in entry, "字段用 candidate——候选数不直接称为人数"


# ---------- U7：命名上下文（lookup / pending_name） ----------


def test_lookup_is_read_only_and_respects_threshold_and_margin(tmp_path):
    store, _, _ = make_store(tmp_path)
    first = store.observe(A, samples_for(4.0, seed=1))
    second = store.observe(B, samples_for(4.0, seed=2))
    assert store.lookup(A) == first.candidate_id
    assert store.lookup(B) == second.candidate_id
    assert store.lookup(MIX) is None, "边际不足（歧义）不绑定命名上下文"
    assert store.lookup(vec(120.0)) is None, "陌生声音（低于指派阈值）返回 None"
    assert store.lookup(np.array([0.0, 0.0], np.float32)) is None, "零向量返回 None"
    assert table_counts(store) == (2, 2), "lookup 只读：不建候选、不写证据"
    assert all(c["status"] == "accumulating" for c in store.snapshot()["candidates"])


def test_set_and_clear_pending_name_only_on_active_candidates(tmp_path):
    store, db, clock = make_store(tmp_path)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    assert store.set_pending_name(v1.candidate_id, "爸爸") is True
    assert candidate_rows(store)[v1.candidate_id]["pending_name"] == "爸爸"
    store.clear_pending_name(v1.candidate_id)
    assert candidate_rows(store)[v1.candidate_id]["pending_name"] is None
    assert store.set_pending_name(999, "妈妈") is False, "候选不存在返回 False"
    # 误绑防线：已失效候选不可再设称呼
    db.close()
    store2, db2, _ = open_store(tmp_path, fingerprint="fp-b:2", clock=clock)
    assert candidate_rows(store2)[v1.candidate_id]["status"] == "stale"
    assert store2.set_pending_name(v1.candidate_id, "爸爸") is False
    db2.close()


# ---------- U7：新名晋升（promote_new_name 统一事务） ----------


def test_promote_new_name_single_transaction_visibility(tmp_path):
    """AE2/事务一致性：成员 + profile + 候选状态一次可见（同一事务提交）。"""
    store, memory = make_memory_store(tmp_path)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    store.set_pending_name(v1.candidate_id, "爸爸")
    member_id = store.promote_new_name(v1.candidate_id, "爸爸",
                                       member_factory=raw_member_factory(memory))
    assert [m["name"] for m in memory.people.list_members()] == ["爸爸"]
    profiles = memory.profiles()
    assert len(profiles) == 1 and profiles[0][0] == member_id
    aggregate = store.db.execute(
        "SELECT aggregate_vector FROM voice_candidates WHERE candidate_id=?",
        (v1.candidate_id,)).fetchone()[0]
    assert np.allclose(profiles[0][1], np.frombuffer(aggregate, np.float32)), \
        "成员声纹 = 候选聚合向量"
    row = candidate_rows(store)[v1.candidate_id]
    assert row["status"] == "promoted" and row["member_id"] == member_id
    assert row["pending_name"] is None and row["proposed_member_id"] is None
    summary = store.snapshot()["summary"]
    assert summary["established_count"] == 1, "晋升后计入已建立人数"
    assert summary["accumulating_count"] == 0, "promoted 不再占活跃名额"
    memory.people.close()


def test_promote_rejects_duplicate_name_and_pending_mismatch(tmp_path):
    store, memory = make_memory_store(tmp_path)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    memory.people.create_member("爸爸")
    store.set_pending_name(v1.candidate_id, "爸爸")
    with pytest.raises(CandidateConflict):
        store.promote_new_name(v1.candidate_id, "爸爸",
                               member_factory=raw_member_factory(memory))
    assert len(memory.people.list_members()) == 1, "重名拒绝：走同名关联路径，不建第二成员"
    assert memory.profiles() == [], "拒绝时零写入（事务回滚）"
    store.clear_pending_name(v1.candidate_id)
    with pytest.raises(CandidateConflict):
        store.promote_new_name(v1.candidate_id, "爸爸",
                               member_factory=raw_member_factory(memory))
    memory.people.close()


def test_promote_missing_candidate_not_found(tmp_path):
    store, memory = make_memory_store(tmp_path)
    with pytest.raises(CandidateNotFound):
        store.promote_new_name(999, "爸爸", member_factory=raw_member_factory(memory))
    memory.people.close()


# ---------- U7：同名关联（create_association_request / confirm_association） ----------


def test_association_request_idempotent_and_console_confirm_promotes(tmp_path):
    store, memory = make_memory_store(tmp_path)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    mama = memory.people.create_member("妈妈")["id"]
    verdict = store.create_association_request(v1.candidate_id, mama)
    assert verdict.outcome == ASSOCIATION_PENDING
    row = candidate_rows(store)[v1.candidate_id]
    assert row["status"] == "awaiting_console" and row["proposed_member_id"] == mama
    assert memory.profiles() == [], "挂请求绝不写 voice_profiles（权限边界）"
    # 幂等：同候选同成员的重复请求不重建
    store.create_association_request(v1.candidate_id, mama)
    assert candidate_rows(store)[v1.candidate_id]["proposed_member_id"] == mama
    assert len(candidate_rows(store)) == 1
    # 控制台确认成功：声纹 = 聚合向量，候选终态 promoted
    result = store.confirm_association(v1.candidate_id, mama)
    assert result == {"ok": True, "member_id": mama, "already": False}
    assert len(memory.profiles()) == 1
    row = candidate_rows(store)[v1.candidate_id]
    assert row["status"] == "promoted" and row["member_id"] == mama
    assert row["proposed_member_id"] is None
    # AE4 重试无重复：已晋升 → 幂等可见，不重复插 profile/成员
    again = store.confirm_association(v1.candidate_id, mama)
    assert again == {"ok": True, "member_id": mama, "already": True}
    assert len(memory.profiles()) == 1 and len(memory.people.list_members()) == 1
    memory.people.close()


def test_confirm_rejects_existing_profile_without_overwrite(tmp_path):
    """AE4：成员已有声纹 → 409 语义冲突拒绝，既有声纹不被覆盖。"""
    store, memory = make_memory_store(tmp_path)
    mama = memory.enroll("妈妈", B)  # 成员 + 既有声纹
    v1 = store.observe(A, samples_for(4.0, seed=1))  # A 与 B cos≈0.64，正常建候选
    store.create_association_request(v1.candidate_id, mama)
    with pytest.raises(CandidateConflict):
        store.confirm_association(v1.candidate_id, mama)
    assert np.allclose(dict(memory.profiles())[mama], B), "既有声纹原样保留"
    assert candidate_rows(store)[v1.candidate_id]["status"] == "awaiting_console"
    memory.people.close()


def test_confirm_rejects_wrong_member_and_missing_request(tmp_path):
    store, memory = make_memory_store(tmp_path)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    mama = memory.people.create_member("妈妈")["id"]
    other = memory.people.create_member("爸爸")["id"]
    # 没有待确认请求 → 冲突
    with pytest.raises(CandidateConflict):
        store.confirm_association(v1.candidate_id, mama)
    store.create_association_request(v1.candidate_id, mama)
    # 不能确认到不同成员
    with pytest.raises(CandidateConflict):
        store.confirm_association(v1.candidate_id, other)
    memory.people.delete_member(mama)
    # 成员已不存在
    with pytest.raises(CandidateNotFound):
        store.confirm_association(v1.candidate_id, mama)
    # 候选不存在
    with pytest.raises(CandidateNotFound):
        store.confirm_association(4242, other)
    memory.people.close()


def test_awaiting_console_stays_active_and_counts_in_snapshot(tmp_path):
    store, memory = make_memory_store(tmp_path)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    mama = memory.enroll("妈妈", B)
    store.create_association_request(v1.candidate_id, mama)
    v2 = store.observe(A, samples_for(4.0, seed=2))
    assert v2.outcome == "accepted" and v2.candidate_id == v1.candidate_id, \
        "同名待确认仍算活跃候选：等待期继续积累证据"
    summary = store.snapshot()["summary"]
    assert summary["awaiting_console_count"] == 1
    assert summary["accumulating_count"] == 0
    entry = store.snapshot()["candidates"][0]
    assert entry["proposed_member_id"] == mama
    memory.people.close()


def test_stale_fingerprint_also_invalidates_awaiting_console(tmp_path):
    clock = Clock(1000.0)
    store, memory = make_memory_store(tmp_path, clock=clock)
    v1 = store.observe(A, samples_for(4.0, seed=1))
    mama = memory.people.create_member("妈妈")["id"]
    store.create_association_request(v1.candidate_id, mama)
    memory.people.close()
    store2, db2, _ = open_store(tmp_path, fingerprint="fp-b:2", clock=clock)
    assert candidate_rows(store2)[v1.candidate_id]["status"] == "stale", \
        "模型/策略变化对同名待确认候选同样失效"
    db2.close()
