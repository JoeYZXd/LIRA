"""多候选声纹积累存储（开发者控制台改版 U6，KTD5/KTD6）。

职责与边界：
  - 在与 LIRA 的日常交谈中，把有效语音段的声纹向量归入稳定候选并持久化，
    使多个声音跨多轮、跨重启积累证据，进度具有稳定含义（R4/R5）；
  - **同一 SQLite 连接**：构造入参为 sqlite3 连接（生产装配传
    ``PersonalMemory.db``，即 ChatStore 连接）——U7 的同名确认晋升必须与
    members/voice_profiles 同事务，分连接做不到。候选表与 members 相互独立：
    不建外键、确认前没有 member_id 权限、不产生个人记忆权限（KTD6）；
  - **绝不存原始音频**：只存归一化向量、段数/时长统计与 sha256 去重标识；
  - 无锁/单循环纪律：与 PersonalMemory 相同，单进程 asyncio 单事件循环线程
    内同步调用（observe 写量小，与 completed 同级），本类不做内部加锁；
  - store 以向量为输入、与提取器无关——测试用固定向量证明行为（Execution note）。

进度语义（KTD5，工程初值不代表识别准确率，U8 校准）：
  - 有效段 = 通过时长/能量/削波质量门、未被去重命中、非歧义、非已建立成员、
    且与所归候选聚合向量一致的段；
  - 进度 = min(段数/目标段数, 秒数/目标秒数)，取较小值；未过段间一致性门
    封顶 99%；计数、时长、两两一致性齐备才置 ready（进度 1.0，仍待身份确认）；
  - 相似度是余弦分数，不是百分比，不与进度混同、不代表识别准确率。

stale / expired / 去重滚动的语义选择：
  - **stale**（置状态、不删除）：打开本类时，模型指纹或策略版本与现任不一致
    的活跃候选一律置 stale——旧证据保留供排查，进度从零重新积累（KTD5：
    策略/模型变化不能静默把旧证据变成新 100%）；observe 只指派现任指纹 +
    现任策略版本的候选；
  - **expired**（惰性置位）：observe/snapshot 开头把超过 ttl 无新证据的
    accumulating 候选置 expired；ready/awaiting_console 候选不过期
    （U7 确认流程负责收尾；久拖不决由控制台显式处理，不静默丢关联）；

候选状态机（U7，KTD6"身份确认和记忆授权分别受门控"）：
    accumulating ──就绪复核──> ready ──┬─ 新名自然确认（promote_new_name）─┐
        │  ▲                           └─ 同名介绍（create_association_request）│
        │  └─ 继续接受有效证据          │                                     │
        ├─> awaiting_console ──控制台确认（confirm_association）──────────────┤
        ├─> stale（指纹/策略变化，终态留痕）                                   │
        ├─> expired（ttl 无新证据，仅 accumulating）                          │
        └──────────────────────────────────> promoted（终态，member_id 回填）
  member_id / proposed_member_id 两列是权限边界的物理体现：确认/晋升事务之外
  恒为 NULL——确认前候选没有成员身份，个人记忆读写保持关闭（KTD6）。
  - **去重标识全局滚动保留最近 100 条**：现存标识命中即 duplicate 拒绝
    （sha256 对样本原始字节，跨重启稳定）；标识被淘汰后重复保护随之失效——
    同片段按新片段重新判定（永久保留全部标识不可行；100 条按写入时间滚动，
    正常使用中极少回绕）。候选自身的 segment_count/total_seconds 不回退。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from lira.audio._paths import SAMPLE_RATE
from lira.audio.activity import CLIP_THRESHOLD
from lira.dev_telemetry import safe_add_event

__all__ = [
    "VoiceCandidates", "Verdict", "CandidateError", "CandidateNotFound",
    "CandidateConflict", "ASSOCIATION_PENDING", "CLIP_THRESHOLD",
    "top1_with_margin",
]

#: 去重标识全局滚动上限（KTD5 数据关系：最多保留最近 100 个标识）
MAX_DEDUP_KEYS = 100

#: 进度封顶：未过段间一致性门时最多 99%（KTD5：条件齐备才 100%）
PROGRESS_CAP = 0.99

# Verdict.outcome 取值（拒绝原因可读、可计数，为 U7 磁贴与 U8 校准留事件面）
ACCEPTED = "accepted"
DUPLICATE = "duplicate"
TOO_SHORT = "too_short"
LOW_ENERGY = "low_energy"
CLIPPED = "clipped"
AMBIGUOUS = "ambiguous"
ESTABLISHED = "established"
PRIVACY = "privacy"
STALE_FINGERPRINT = "stale_fingerprint"
BOUND_REACHED = "bound_reached"
INCONSISTENT = "inconsistent"
ASSOCIATION_PENDING = "association_pending"

# 候选状态单一出处（U8 评审：原先散布 SQL 字面量 ~14 处，与 ACTIVE_STATUSES
# 可静默分叉——所有 IN 子句与状态比较皆由此组合）
STATUS_ACCUMULATING = "accumulating"
STATUS_READY = "ready"
STATUS_AWAITING_CONSOLE = "awaiting_console"
STATUS_STALE = "stale"
STATUS_EXPIRED = "expired"
STATUS_PROMOTED = "promoted"
#: 可继续积累证据 / 可被命名晋升的"活跃"集合
ACTIVE_STATUSES = (STATUS_ACCUMULATING, STATUS_READY, STATUS_AWAITING_CONSOLE)
#: 可被自然命名晋升（awaiting_console 已挂同名请求，不再走新名路径）
NAMEABLE_STATUSES = (STATUS_ACCUMULATING, STATUS_READY)


class CandidateError(Exception):
    """候选/确认操作失败基类（dev 端点按子类映射 404/409，不记内容）。"""


class CandidateNotFound(CandidateError):
    """候选或其挂载的成员/请求不存在（404 语义）。"""


class CandidateConflict(CandidateError):
    """状态不符或成员已有声纹——拒绝且不覆盖（409 语义，AE4）。"""


def top1_with_margin(scored, threshold, margin):
    """「top-1 达标 + 对次高边际」统一判定（U8 审查收口，原先 4 处手写、
    靠注释互称同则——含 SpeakerIdentity.match）。

    scored 为 [(score, payload)] 任意顺序；按分数降序（稳定）后：
    top1 ≥ threshold 且（无次高或 top1−top2 ≥ margin）→ (payload, False)；
    top1 达标但边际不足 → (None, True)（歧义：声音归属不明确）；
    无达标 → (None, False)。
    """
    if not scored:
        return None, False
    ordered = sorted(scored, key=lambda item: item[0], reverse=True)
    if ordered[0][0] < threshold:
        return None, False
    if len(ordered) > 1 and ordered[0][0] - ordered[1][0] < margin:
        return None, True
    return ordered[0][1], False


@dataclass(frozen=True)
class Verdict:
    """单次观察的判定结果。

    本单元（U6）不驱动对话行为；U7 磁贴与确认流程才消费。``progress`` 仅在
    accepted 时给出（该候选更新后的采集完成度）。
    """

    outcome: str
    candidate_id: int | None = None
    progress: float | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.outcome == ACCEPTED


class VoiceCandidates:
    """候选声纹存储（U6）：observe 记一段证据，snapshot 出 U7 磁贴数据面。"""

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        fingerprint: str,
        profiles_provider: Callable[[], list],
        params,
        privacy: Callable[[], bool],
        now: Callable[[], float] | None = None,
        event_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self.db = db
        self.fingerprint = str(fingerprint)
        self.params = params
        self._profiles_provider = profiles_provider
        self._privacy = privacy
        self._now = time.time if now is None else now
        self._event_sink = event_sink
        # 幂等建表（KTD8）：新表不触碰既有 members/memory_turns；候选与 members
        # 不建外键——member_id 列只在确认/晋升事务内回填，确认前恒 NULL，
        # 这是"候选不产生成员权限"（KTD6）的物理体现。U7 追加三列：未发布的
        # 开发库分支内直接改 CREATE TABLE，对 U6 时代已存在的库用 ALTER 兜底
        # （取简：幂等加列，无需迁移框架；失败重开即重建候选可重新积累）。
        # voice_profiles 是 PersonalMemory 的既有表（其 DDL 唯一出处）——确认
        # 事务往里写聚合向量时表必已存在（生产装配共享同一连接且 PersonalMemory
        # 先建；U8 化简审查：此处不再复制 DDL，防双定义静默漂移）。
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS voice_candidates (
                candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                aggregate_vector BLOB NOT NULL,
                model_fingerprint TEXT NOT NULL,
                segment_count INTEGER NOT NULL DEFAULT 0,
                total_seconds REAL NOT NULL DEFAULT 0,
                consistency_passed INTEGER NOT NULL DEFAULT 0,
                strategy_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                pending_name TEXT,
                proposed_member_id INTEGER,
                member_id INTEGER);
            CREATE TABLE IF NOT EXISTS voice_candidate_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES voice_candidates(candidate_id),
                dedup_key TEXT NOT NULL,
                duration REAL NOT NULL,
                vector BLOB NOT NULL,
                created_at REAL NOT NULL);
        ''')
        for column in ("pending_name TEXT", "proposed_member_id INTEGER", "member_id INTEGER"):
            try:
                self.db.execute(f"ALTER TABLE voice_candidates ADD COLUMN {column}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise  # 非重复列的 OperationalError 不吞（U8 评审：过宽 except）
        self._invalidate_foreign()

    # ---------- 生命周期 ----------

    def _invalidate_foreign(self) -> None:
        """stale 语义（打开时）：指纹或策略版本与现任不符的活跃候选置 stale。

        不删除、不混算：旧证据保留供排查，进度从零重新积累。
        """
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        with self.db:
            self.db.execute(
                f"UPDATE voice_candidates SET status={STATUS_STALE!r}, updated_at=? "
                f"WHERE status IN ({placeholders}) "
                "AND (model_fingerprint != ? OR strategy_version != ?)",
                (self._now(), *ACTIVE_STATUSES, self.fingerprint,
                 self.params.strategy_version))

    def _expire_candidates(self, now: float) -> None:
        """惰性过期：accumulating 候选超过 ttl 无新证据 → expired。

        U8 效率审查：先只读探测（绝大多数时刻 0 行命中），避免 observe/
        snapshot 每次都开一次空写事务、与主循环在共享连接上互相插锁。
        """
        cutoff = now - self.params.candidate_ttl_days * 86400.0
        due = self.db.execute(
            "SELECT 1 FROM voice_candidates "
            f"WHERE status={STATUS_ACCUMULATING!r} AND updated_at < ? LIMIT 1", (cutoff,)).fetchone()
        if due is None:
            return
        with self.db:
            self.db.execute(
                f"UPDATE voice_candidates SET status={STATUS_EXPIRED!r} "
                f"WHERE status={STATUS_ACCUMULATING!r} AND updated_at < ?", (cutoff,))

    # ---------- 观察入口 ----------

    def observe(self, vector, samples, *, now: float | None = None) -> Verdict:
        """记录一段已完成提取的声纹证据（sync；主循环复检通过后才调用）。

        判定顺序：隐私 -> 向量维度 -> 质量门（时长/能量/削波）-> 去重 ->
        已建立成员 -> 候选指派 -> 段一致性 -> 接受与就绪复核。任何拒绝都
        不产生写入（AE2：低质量、重复或歧义片段不增加进度）。
        """
        now = self._now() if now is None else now
        # 迟到提交第二道防线（AE5）：主流程复检是第一道，这里再看一眼隐私。
        # U8 COR-8：隐私闸先于 expire——承诺顺序是"隐私期零写入"，expire 的
        # 状态翻转也是写入。
        if self._privacy():
            return self._verdict(PRIVACY, reason="隐私开启，候选证据暂停写入")
        self._expire_candidates(now)
        vector = np.asarray(vector, np.float32)
        norm = float(np.linalg.norm(vector))
        vector = vector / norm if norm > 1e-6 else vector
        dim = self._fingerprint_dim()
        if dim and vector.shape != (dim,):
            # 指纹应捕捉而未捕捉的模型变化：绝不与现任候选混算
            return self._verdict(STALE_FINGERPRINT,
                                 reason="向量维度与现任模型指纹不符，需要重新积累")

        samples = np.ascontiguousarray(np.asarray(samples, np.float32))
        duration = samples.size / SAMPLE_RATE
        if duration < self.params.min_segment_seconds:
            return self._verdict(TOO_SHORT,
                                 reason=f"片段 {duration:.2f}s 短于最小 {self.params.min_segment_seconds:g}s")
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        if rms < self.params.min_rms:
            return self._verdict(LOW_ENERGY,
                                 reason=f"能量 RMS {rms:.4f} 低于 {self.params.min_rms:g}")
        clip_ratio = float(np.mean(np.abs(samples) >= CLIP_THRESHOLD))
        if clip_ratio > self.params.max_clip_ratio:
            return self._verdict(CLIPPED,
                                 reason=f"削波比例 {clip_ratio:.2%} 超过 {self.params.max_clip_ratio:.0%}")

        # 跨重启稳定的去重标识：样本原始字节 sha256（不用 utterance_id）
        dedup_key = hashlib.sha256(samples.tobytes()).hexdigest()
        if self.db.execute("SELECT 1 FROM voice_candidate_evidence WHERE dedup_key=? LIMIT 1",
                           (dedup_key,)).fetchone():
            return self._verdict(DUPLICATE, reason="重复片段不重复累计")

        established = self._match_established(vector)
        if established is not None:
            member_id, ambiguous = established
            if ambiguous:
                return self._verdict(AMBIGUOUS, reason="声音归属不明确，本段不计入")
            # U8 COR-3：established 的 id 是 member_id——不进 candidate_id 字段
            #（遥测文本会把它误记成候选 id，U8 校准按候选聚合会误计）
            return self._verdict(ESTABLISHED,
                                 reason=f"命中已建立声纹 member={member_id}，走现有识别，不建候选")

        target = self._assign(vector, now)
        if isinstance(target, Verdict):
            return target
        candidate_id, aggregate_blob, segment_count, total_seconds = target
        aggregate = np.frombuffer(aggregate_blob, np.float32)
        if float(np.dot(vector, aggregate)) < self.params.consistency_threshold:
            return self._verdict(INCONSISTENT, candidate_id=candidate_id,
                                 reason="与该候选既有证据一致性不足，本段不计入")

        merged = aggregate * segment_count + vector
        merged_norm = float(np.linalg.norm(merged))
        merged = merged / merged_norm if merged_norm > 1e-6 else vector
        new_count = segment_count + 1
        new_seconds = total_seconds + duration
        with self.db:
            self.db.execute(
                "INSERT INTO voice_candidate_evidence"
                "(candidate_id,dedup_key,duration,vector,created_at) VALUES (?,?,?,?,?)",
                (candidate_id, dedup_key, duration, vector.tobytes(), now))
            self.db.execute(
                "UPDATE voice_candidates SET aggregate_vector=?, segment_count=?, "
                "total_seconds=?, updated_at=? WHERE candidate_id=?",
                (merged.astype(np.float32).tobytes(), new_count, new_seconds, now, candidate_id))
            self.db.execute(
                "DELETE FROM voice_candidate_evidence WHERE id NOT IN "
                "(SELECT id FROM voice_candidate_evidence ORDER BY id DESC LIMIT ?)",
                (MAX_DEDUP_KEYS,))
            # 就绪复核（同事务，KTD5）：计数、时长与现存证据两两一致性齐备才
            # ready。U8 效率审查：只在未过门且计数时长达标时才做 O(n²) 复核
            # （已 passed 或未达标阶段，全量证据拉取 + 两两 dot 是纯浪费）。
            passed = bool(self.db.execute(
                "SELECT consistency_passed FROM voice_candidates WHERE candidate_id=?",
                (candidate_id,)).fetchone()[0])
            if (not passed and new_count >= self.params.target_segments
                    and new_seconds >= self.params.target_seconds):
                worst = self._min_pairwise_cos(candidate_id)
                if worst is not None and worst >= self.params.consistency_threshold:
                    self.db.execute(
                        f"UPDATE voice_candidates SET consistency_passed=1, status={STATUS_READY!r} "
                        "WHERE candidate_id=?", (candidate_id,))
                    passed = True
        return self._verdict(ACCEPTED, candidate_id=candidate_id,
                             progress=self._progress(new_count, new_seconds, passed),
                             reason=f"有效证据 {new_count} 段 / {new_seconds:.1f}s")

    # ---------- 快照（U7 磁贴数据面） ----------

    def snapshot(self, *, now: float | None = None, established=None) -> dict:
        """候选与汇总快照。字段用 candidate（候选数不直接称为人数）。

        U7：每候选带 pending_name/proposed_member_id/member_id/status；
        established_count 只统计真实 voice_profiles（AE2：采集 100% 但未
        确认的候选不计已建立人数）。established 可传入已取的 profile 列表
        （U8 效率审查：dev 端点同请求内避免第二次全表 BLOB 解码）。
        """
        now = self._now() if now is None else now
        # 隐私期只读（U8 COR-1/AE5）：expire 是写事务——磁贴展开触发的快照
        # 不得在隐私期落任何写入（过期收尾推迟到下一次非隐私 observe）
        if not self._privacy():
            self._expire_candidates(now)
        rows = self.db.execute(
            "SELECT candidate_id,status,segment_count,total_seconds,"
            "consistency_passed,updated_at,pending_name,proposed_member_id,"
            "member_id FROM voice_candidates "
            "ORDER BY candidate_id").fetchall()
        candidates = []
        for row in rows:
            passed = bool(row[4])
            candidates.append({
                "candidate_id": int(row[0]),
                "status": str(row[1]),
                "progress": self._progress(int(row[2]), float(row[3]), passed),
                "segment_count": int(row[2]),
                "total_seconds": float(row[3]),
                "targets": {"segments": self.params.target_segments,
                            "seconds": self.params.target_seconds},
                "consistency_passed": passed,
                "updated_at": float(row[5]),
                "pending_name": row[6],
                "proposed_member_id": row[7],
                "member_id": row[8],
            })
        if established is None:
            established = self._profiles_provider() or []
        return {
            "candidates": candidates,
            "summary": {
                "established_count": len(established),
                "accumulating_count": sum(1 for c in candidates if c["status"] == "accumulating"),
                "ready_count": sum(1 for c in candidates if c["status"] == "ready"),
                "awaiting_console_count": sum(
                    1 for c in candidates if c["status"] == "awaiting_console"),
            },
        }

    # ---------- 命名上下文与确认晋升（U7，KTD6） ----------

    def delete_unfinished(self, candidate_id: int) -> None:
        """Delete only unbound candidate evidence, never an established identity."""
        with self.db:
            row=self._candidate_row(candidate_id)
            if row is None:
                raise CandidateNotFound('声纹候选不存在')
            if row['member_id'] is not None or row['status'] == STATUS_PROMOTED:
                raise CandidateConflict('已建立的声纹不能作为未完成记录删除')
            self.db.execute('DELETE FROM voice_candidate_evidence WHERE candidate_id=?',(candidate_id,))
            self.db.execute('DELETE FROM voice_candidates WHERE candidate_id=?',(candidate_id,))
        self._emit('deleted', candidate_id=candidate_id)

    def _candidate_row(self, candidate_id: int):
        return self.db.execute(
            "SELECT * FROM voice_candidates WHERE candidate_id=?",
            (candidate_id,)).fetchone()

    def lookup(self, vector) -> int | None:
        """当前声音对应的活跃候选 id（命名上下文绑定用）；**只读不变更**。

        与 _assign 同则（经 top1_with_margin 收口），但绝不建候选、置 stale
        或写任何行——歧义（边际不足）与陌生声音都返回 None，命名上下文
        宁缺毋错绑。每轮 resolve 都会调用，故不发遥测事件。
        """
        vector = np.asarray(vector, np.float32)
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-6:
            return None
        vector = vector / norm
        scored = []
        for row in self._active_rows():
            aggregate = np.frombuffer(row[1], np.float32)
            if aggregate.shape != vector.shape:
                continue  # 维度不符（防御）：跳过，lookup 不做任何置失效写
            scored.append((float(np.dot(vector, aggregate)), int(row[0])))
        hit, _ambiguous = top1_with_margin(scored, self.params.assign_threshold,
                                            self.params.match_margin)
        return hit

    def set_pending_name(self, candidate_id: int, name: str,
                         *, now: float | None = None) -> bool:
        """把自然对话中给出的称呼暂存到候选（可命名状态才收）；失效返回 False。

        误绑防线之一：候选已 stale/expired/promoted 时不设——名字只绑当前
        仍在积累的活跃候选。纯暂存，不创建成员、不写声纹。
        """
        row = self._candidate_row(candidate_id)
        if row is None or row["status"] not in NAMEABLE_STATUSES:
            return False
        with self.db:
            self.db.execute(
                "UPDATE voice_candidates SET pending_name=?, updated_at=? "
                "WHERE candidate_id=?",
                (str(name), self._now() if now is None else now, candidate_id))
        return True

    def clear_pending_name(self, candidate_id: int, *, now: float | None = None) -> None:
        """清暂存称呼（隐私开启 / 会话上下文作废时调用，防迟到回执错绑）。"""
        with self.db:
            self.db.execute(
                "UPDATE voice_candidates SET pending_name=NULL, updated_at=? "
                "WHERE candidate_id=?",
                (self._now() if now is None else now, candidate_id))

    def create_association_request(self, candidate_id: int, member_id: int,
                                   *, now: float | None = None) -> Verdict:
        """同名候选 → 挂"待控制台确认"请求；幂等；**绝不写 voice_profiles**。

        AE4/KTD6：同名成员关联只能由鉴权控制台确认，会话内确认不登记；
        本操作只标记意图（proposed_member_id + awaiting_console），权限边界
        （member_id 列）不动。
        """
        now = self._now() if now is None else now
        self._expire_candidates(now)
        row = self._candidate_row(candidate_id)
        if row is None:
            raise CandidateNotFound(f"候选不存在: {candidate_id}")
        if row["status"] == "awaiting_console" and row["proposed_member_id"] == member_id:
            return self._verdict(ASSOCIATION_PENDING, candidate_id=candidate_id,
                                 reason="同名关联待控制台确认（重复请求不重复生成）")
        if row["status"] not in ACTIVE_STATUSES:
            raise CandidateConflict(
                f"候选 {candidate_id} 状态 {row['status']} 不再活跃，无法挂关联请求")
        if self.db.execute("SELECT 1 FROM members WHERE id=?", (member_id,)).fetchone() is None:
            raise CandidateNotFound(f"成员不存在: {member_id}")
        with self.db:
            self.db.execute(
                f"UPDATE voice_candidates SET status={STATUS_AWAITING_CONSOLE!r}, "
                "proposed_member_id=?, pending_name=NULL, updated_at=? "
                "WHERE candidate_id=?", (int(member_id), now, candidate_id))
        return self._verdict(ASSOCIATION_PENDING, candidate_id=candidate_id,
                             reason="同名关联已生成，等待开发者控制台确认")

    def promote_new_name(self, candidate_id: int, name: str, *,
                         member_factory: Callable[[str], int],
                         now: float | None = None) -> int:
        """新称呼自然确认 → 统一事务晋升：建成员 + 写声纹 + 候选置 promoted。

        事务载体（KTD6"身份确认 API 与语音晋升使用同一领域操作及事务"）：
        ``with self.db`` 单事务包住 member_factory（members 插入）+
        voice_profiles（候选聚合向量落为成员声纹）+ 候选状态/回填三步，
        任一步失败整体回滚——成员列表、profile 与候选状态一次可见、一致。
        member_factory 必须在**调用方连接上**做纯插入（不自带 commit），
        否则会在外层事务中途提交、破坏原子性（ChatStore.create_member
        自带 ``with conn``，故由调用方适配内联插入）。

        拒绝条件：候选不存在 / 非可命名状态 / pending_name 不匹配（名字
        只晋升给给过这个名字的候选——误绑防线）/ 成员重名（走 association
        路径）。成功后候选终态 promoted、member_id 回填（权限边界的开启
        时刻），pending_name/proposed_member_id 清空。
        """
        now = self._now() if now is None else now
        self._expire_candidates(now)
        row = self._candidate_row(candidate_id)
        if row is None:
            raise CandidateNotFound(f"候选不存在: {candidate_id}")
        if row["status"] not in NAMEABLE_STATUSES:
            raise CandidateConflict(
                f"候选 {candidate_id} 状态 {row['status']} 不可命名晋升")
        if row["pending_name"] != str(name):
            raise CandidateConflict("候选暂存称呼与晋升名字不符，拒绝晋升（防错绑）")
        with self.db:
            try:
                member_id = int(member_factory(str(name)))
            except sqlite3.IntegrityError as exc:
                raise CandidateConflict(f"成员名已存在，走同名关联路径: {name}") from exc
            self.db.execute("INSERT INTO voice_profiles(member_id, vector) VALUES (?,?)",
                            (member_id, row["aggregate_vector"]))
            self.db.execute(
                f"UPDATE voice_candidates SET status={STATUS_PROMOTED!r}, member_id=?, "
                "pending_name=NULL, proposed_member_id=NULL, updated_at=? "
                "WHERE candidate_id=?", (member_id, now, candidate_id))
        self._verdict(ACCEPTED, candidate_id=candidate_id,
                      reason=f"候选 {candidate_id} 已晋升为成员 {member_id}")
        return member_id

    def confirm_association(self, candidate_id: int, member_id: int,
                            *, now: float | None = None) -> dict:
        """控制台确认同名关联 → 统一事务：写声纹 + 候选置 promoted。

        核对（全部通过才写）：候选处于 awaiting_console 且
        proposed_member_id==member_id；**成员已有 voice_profile → 冲突拒绝，
        绝不覆盖**（AE4）。重试已晋升且指向同一成员 → 幂等返回 already（不
        重复插 profile/成员）；指向不同成员 → 冲突。成功则同事务插入
        voice_profiles(aggregate) 并置 promoted、member_id 回填、清 proposed。
        """
        now = self._now() if now is None else now
        self._expire_candidates(now)
        row = self._candidate_row(candidate_id)
        if row is None:
            raise CandidateNotFound(f"候选不存在: {candidate_id}")
        if row["status"] == "promoted":
            if row["member_id"] == member_id:
                return {"ok": True, "member_id": int(member_id), "already": True}
            raise CandidateConflict("候选已关联到其他成员，不能重复确认")
        if row["status"] != "awaiting_console" or row["proposed_member_id"] is None:
            raise CandidateConflict(f"候选 {candidate_id} 没有待确认的同名关联")
        if row["proposed_member_id"] != member_id:
            raise CandidateConflict("待确认关联指向其他成员，不能确认到不同成员")
        if self.db.execute("SELECT 1 FROM members WHERE id=?", (member_id,)).fetchone() is None:
            raise CandidateNotFound(f"成员不存在: {member_id}")
        if self.db.execute("SELECT 1 FROM voice_profiles WHERE member_id=?",
                           (member_id,)).fetchone() is not None:
            raise CandidateConflict("该成员已有声纹记录，确认被拒绝（不覆盖既有声纹）")
        with self.db:
            self.db.execute("INSERT INTO voice_profiles(member_id, vector) VALUES (?,?)",
                            (member_id, row["aggregate_vector"]))
            self.db.execute(
                f"UPDATE voice_candidates SET status={STATUS_PROMOTED!r}, member_id=?, "
                "pending_name=NULL, proposed_member_id=NULL, updated_at=? "
                "WHERE candidate_id=?", (int(member_id), now, candidate_id))
        self._verdict(ACCEPTED, candidate_id=candidate_id,
                      reason=f"控制台确认：候选 {candidate_id} 已关联成员 {member_id}")
        return {"ok": True, "member_id": int(member_id), "already": False}

    # ---------- 判定辅助 ----------

    def _fingerprint_dim(self) -> int:
        """从指纹后缀解析维度（SpeakerIdentity.model_fingerprint 形如 ``<sha1>:<dim>``）。"""
        _, _, dim = self.fingerprint.rpartition(":")
        try:
            return int(dim)
        except ValueError:
            return 0

    def _match_established(self, vector):
        """已建立成员判定（与 SpeakerIdentity.match 同则，经 top1_with_margin
        收口——"同则"由共用实现保证而非注释）。

        最高 cos >= match_threshold 且对次高边际 >= match_margin →
        (member_id, False)；最高达标但边际不足 → (None, True) 表示歧义；
        无达标 → None。旧模型时代的 profile（维度不符）不参与混算。
        """
        scores = []
        for member, member_vector in (self._profiles_provider() or []):
            member_vector = np.asarray(member_vector, np.float32)
            if member_vector.shape != vector.shape:
                continue
            scores.append((float(np.dot(vector, member_vector)), int(member)))
        hit, ambiguous = top1_with_margin(scores, self.params.match_threshold,
                                           self.params.match_margin)
        if hit is None:
            return (None, True) if ambiguous else None
        return hit, False

    def _active_rows(self):
        """现任指纹 + 现任策略版本的活跃候选（observe 只在这些里指派）。

        awaiting_console（同名待确认）仍算活跃：确认等待期继续积累证据，
        但它已挂请求，不走新名晋升路径（见 NAMEABLE_STATUSES）。
        """
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        return self.db.execute(
            "SELECT candidate_id,aggregate_vector,segment_count,total_seconds "
            f"FROM voice_candidates WHERE status IN ({placeholders}) "
            "AND model_fingerprint=? AND strategy_version=? ORDER BY candidate_id",
            (*ACTIVE_STATUSES, self.fingerprint, self.params.strategy_version)).fetchall()

    def _count_active(self) -> int:
        """现任指纹 + 现任策略版本的活跃候选数（max_candidates 名额口径；
        上限 20，行数极小，直接复用 _active_rows 免复制 WHERE——U8 审查）。"""
        return len(self._active_rows())

    def _assign(self, vector, now: float):
        """候选指派：现任活跃候选按 cos 降序（经 top1_with_margin 收口）——
        最高达标且边际足 → 归入；最高达标但边际不足 → 歧义拒绝；
        无达标 → 新建候选（未满上限时）。

        返回 (candidate_id, aggregate_blob, segment_count, total_seconds) 或 Verdict。
        """
        scored = []
        for row in self._active_rows():
            aggregate = np.frombuffer(row[1], np.float32)
            if aggregate.shape != vector.shape:
                # 指纹未反映的维度变化（防御）：候选立即失效，绝不与本段混算
                with self.db:
                    self.db.execute(f"UPDATE voice_candidates SET status={STATUS_STALE!r} "
                                    "WHERE candidate_id=?", (row[0],))
                continue
            scored.append((float(np.dot(vector, aggregate)), row))
        hit, ambiguous = top1_with_margin(scored, self.params.assign_threshold,
                                           self.params.match_margin)
        if ambiguous:
            return Verdict(AMBIGUOUS, reason="声音归属不明确，本段不计入任何候选")
        if hit is not None:
            return hit
        if self._count_active() >= self.params.max_candidates:
            return Verdict(BOUND_REACHED,
                           reason="未绑定候选已达上限，新声音暂不积累（等待确认或过期腾出名额）")
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO voice_candidates(aggregate_vector,model_fingerprint,"
                "segment_count,total_seconds,consistency_passed,strategy_version,"
                "status,created_at,updated_at) VALUES (?, ?, 0, 0, 0, ?, ?, ?, ?)",
                (vector.tobytes(), self.fingerprint, self.params.strategy_version,
                 STATUS_ACCUMULATING, now, now))
        return cursor.lastrowid, vector.tobytes(), 0, 0.0

    def _min_pairwise_cos(self, candidate_id):
        """该候选现存证据两两余弦的最小值（就绪复核用）；证据不足返回 None。

        去重标识滚动淘汰后现存证据可能少于累计段数——复核基于现存证据，
        docstring「去重滚动」一节有说明。
        """
        vectors = [np.frombuffer(row[0], np.float32)
                   for row in self.db.execute(
                       "SELECT vector FROM voice_candidate_evidence WHERE candidate_id=?",
                       (candidate_id,))]
        if len(vectors) < self.params.target_segments:
            return None
        worst = 1.0
        for i in range(len(vectors)):
            for j in range(i + 1, len(vectors)):
                worst = min(worst, float(np.dot(vectors[i], vectors[j])))
        return worst

    def _progress(self, count: int, seconds: float, consistency_passed: bool) -> float:
        """采集完成度 = min(段数比, 时长比)；未过一致性门封顶 99%（KTD5）。"""
        ratio = min(count / self.params.target_segments,
                    seconds / self.params.target_seconds)
        ratio = max(0.0, min(ratio, 1.0))
        if not consistency_passed:
            ratio = min(ratio, PROGRESS_CAP)
        return round(ratio, 4)

    def _emit(self, outcome: str, *, candidate_id: int | None = None,
              reason: str = "") -> None:
        """纯事件发射（U8 评审：promote/confirm 成功路径丢弃 Verdict 返回值，
        语义是发事件而非构造结果——单独立名避免读似死代码）。"""
        text = f"{outcome} candidate={candidate_id} {reason}".strip()
        safe_add_event(self._event_sink, "voice_candidate", text)

    def _verdict(self, outcome: str, *, candidate_id: int | None = None,
                 progress: float | None = None, reason: str = "") -> Verdict:
        """构造 Verdict 并发射 voice_candidate 事件（判定链拒绝/接受面）。"""
        verdict = Verdict(outcome, candidate_id=candidate_id,
                          progress=progress, reason=reason)
        self._emit(outcome, candidate_id=candidate_id, reason=reason)
        return verdict
