"""开发者控制台结构化遥测（U2）：有界音频包络环 + 有界文本事件环 + 游标读取面。

定位与纪律：
  - 这是唯一输入的**观察旁路**（PrivacyGatedSink 之后的 ``RouteDispatch.feed``
    逐块统计旁调用 ``on_audio``），不是音频链路的一部分：只读样本、绝不变更
    主链路控制流；观测不打开第二个音频流，也不施加反压——计算超预算的最坏
    后果只是遥测自己的环少几帧。
  - 与 ``voice_log.VOICE_LOG`` / ``main.RING_LOG_HANDLER`` 同一先例：单事件
    循环线程内读写，无锁。公开方法只做 O(环上限) 的内存操作，不做 I/O。
  - 内存硬上限：音频环 100 帧（100ms 桶/gap 标记混排，约 10 秒）；文本环
    500 条且 utf-8 总字节 ≤ 512KiB，双界淘汰最旧。
  - 实例身份：构造时生成 ``instance_id``（uuid4），进程重启即变化。前端凭
    instance_id + 游标判断 reset，不能拿旧数据补出不存在的声音/回复。
  - 不能制造假静音：采集暂停/中断/代次变化都落 ``kind="gap"`` 标记，而不是
    0 电平桶。
  - 隐私联锁（AE5）：``apply_privacy(True)`` 清空两环并拒收后续写入——与
    ``privacy.subscribe`` 的 ``(bool)`` 订阅者契约同签名；主循环装配在
    ``self._gate.apply`` 之后注册（先关音频门，再清遥测缓冲），隐私开启期间
    音频门本就不再喂 ``on_audio``，此处再拒收一次，迟到结果无新写入的双保险。

游标语义（U4 的 observe API 直接消费）：
  - ``audio_since`` / ``events_since`` 返回 ``(items, next_seq, reset)``；
  - cursor 表示"已见到的最大 seq"，返回 seq > cursor 的条目；
  - reset=True（实例不匹配 / 游标越界 / 请求区间已被淘汰或清空）时返回当前
    整窗快照，客户端必须整体重建，一次往返完成重置；
  - seq 计数器永不回退，隐私清空/淘汰通过"cursor+1 < 最旧现存 seq"被辨认。
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass

import numpy as np

from lira.audio._paths import SAMPLE_RATE

__all__ = ["AudioFrame", "TelemetryEvent", "DevTelemetry", "owner_text"]

#: 每桶样本数：100ms @ 16kHz。桶边界按样本数划分，与输入 chunk 大小解耦。
_BUCKET_SAMPLES = SAMPLE_RATE // 10
#: 音频环上限：100 帧（桶/gap 混排）≈ 最近 10 秒。
_MAX_AUDIO_FRAMES = 100
#: 文本事件双界：条数与 utf-8 字节数同时满足，超限从最旧淘汰。
_MAX_TEXT_EVENTS = 500
_MAX_TEXT_BYTES = 512 * 1024
#: 削波判阈（与 RouteDispatch 每秒统计同阈：|x| >= 0.999 记削波）。
_CLIP_THRESHOLD = 0.999


def owner_text(owner: object) -> str | None:
    """OperationOwner → "epoch.operation_id" 事件字符串（U3/KTD3 关联字段）。

    None → None（无归属）；异形 owner 安全降级 str()——鸭子类型，事件面
    不因装配层传入对象形态差异而失败。
    """
    if owner is None:
        return None
    epoch = getattr(owner, "epoch", None)
    operation_id = getattr(owner, "operation_id", None)
    if epoch is None or operation_id is None:
        return str(owner)
    return f"{epoch}.{operation_id}"


@dataclass
class AudioFrame:
    """音频环条目：包络桶（kind="envelope"）或间隔标记（kind="gap"）。

    两者共用同一 ``seq`` 序列；min/max/rms/peak/clip_ratio 仅 envelope 有值。
    """

    seq: int
    t_mono: float
    ts: float
    capture_generation: int
    route: str
    tts_occupied: bool
    kind: str
    reason: str | None = None
    min: float | None = None
    max: float | None = None
    rms: float | None = None
    peak: float | None = None
    clip_ratio: float | None = None


@dataclass
class TelemetryEvent:
    """文本事件（U2 建立字段模式；utterance/turn/owner 关联由 U3 填充）。"""

    seq: int
    instance_id: str
    ts: float
    t_mono: float
    capture_generation: int | None
    kind: str
    text: str
    utterance_id: int | None = None
    turn_id: int | None = None
    operation_owner: str | None = None


class DevTelemetry:
    """有界结构化遥测（单事件循环线程内读写，无锁）。

    - 音频包络环：100ms 桶按样本数划界（chunk 非对齐也正确），generation
      变化自动闭合未满桶并落 gap 标记；
    - 文本事件环：作为 ``VOICE_LOG`` 的镜像 sink + 显式 ``add_event``；
    - 观察者计数为 0 时跳过波形摘要计算（不分配、不算数），文本事件与
      gap 标记不受影响；
    - 隐私开启期间 ``on_audio`` / ``add_event`` 一律拒收。
    """

    def __init__(self) -> None:
        self.instance_id = uuid.uuid4().hex
        self._audio_frames: deque[AudioFrame] = deque(maxlen=_MAX_AUDIO_FRAMES)
        self._events: deque[TelemetryEvent] = deque()
        self._text_bytes = 0
        #: 已分配的最大 seq（含 gap，永不回退；0 = 还没有任何帧，首帧为 1）
        self._audio_seq = 0
        self._event_seq = 0
        self._observers = 0
        #: 轮询活动窗（note_observer_activity 续期；monotonic 过期即无观察者）
        self._observer_until = 0.0
        self._privacy_on = False
        #: 最近一次 on_audio 见到的采集代次/路由/播报占用（gap 帧回填用）
        self._capture_generation = 0
        self._last_route = ""
        self._last_occupied = False
        #: 未满桶的滚动聚合（按样本增量累计，不缓存样本数组）
        self._acc_samples = 0
        self._acc_energy = 0.0
        self._acc_min = math.inf
        self._acc_max = -math.inf
        self._acc_clipped = 0

    # ---------- 观察者（KTD2：无观察者不分配、不计算） ----------

    def add_observer(self) -> None:
        self._observers += 1

    def remove_observer(self) -> None:
        if self._observers > 0:
            self._observers -= 1
        if not self._observers:
            # 归零即丢弃未满桶：未观察窗口前的零散样本不混入下次计算的桶
            self._reset_acc()

    def note_observer_activity(self) -> None:
        """页面轮询即观察（U5 接线）：把"有观察者"状态延长到 monotonic+2s。

        与手动 add_observer/remove_observer 计数并存（任一为真即计算包络）。
        observe 端点每次拉取调用一次——页面停拉（折叠/隐藏/断开）后 2s 内
        包络计算自然停止，符合 KTD2"无观察者不计算"，无需生命周期回调。
        """
        self._observer_until = time.monotonic() + 2.0

    @property
    def observer_count(self) -> int:
        if self._observers > 0:
            return self._observers
        return 1 if time.monotonic() < self._observer_until else 0

    # ---------- 音频旁路 ----------

    def on_audio(self, samples, *, generation, route, tts_occupied) -> None:
        """主输入旁路：只读样本，逐 100ms 桶聚合包络。

        generation 变化即自动闭合上一段（未满桶按旧代次落盘）并标记
        gap("generation_changed")——采集代次边界可辨认，不制造假静音。
        桶的 route/tts_occupied 以完成该桶的那次调用为准（跨块桶极少见，
        只影响标签不影响数值）。
        """
        route_text = str(getattr(route, "value", route))
        occupied = bool(tts_occupied)
        if generation != self._capture_generation:
            self._flush_bucket(self._capture_generation, self._last_route,
                               self._last_occupied)
            # 隐私期间代次照常跟踪（gap 归因不漂移），但不落帧——与下方
            # 拒收一致：on_audio 在隐私期零写入（AE5 迟到结果无新写入）
            if not self._privacy_on:
                self._append_gap("generation_changed", generation, route_text,
                                 occupied)
            self._capture_generation = generation
        self._last_route, self._last_occupied = route_text, occupied
        if self._privacy_on:
            return
        samples = np.asarray(samples, dtype=np.float32)
        pos, n = 0, samples.size
        if not n or not self.observer_count:
            # 无观察者（手动计数为 0 且轮询活动窗已过）：不分配不计算；
            # 顺手丢弃未满桶——未观察窗口的零散样本不混入恢复后的首桶
            self._reset_acc()
            return
        # 桶边界按样本数划分：跨块/超大块都精确切到 1600 样本
        while pos < n:
            take = min(_BUCKET_SAMPLES - self._acc_samples, n - pos)
            seg = samples[pos:pos + take]
            self._acc_samples += take
            self._acc_energy += float(np.sum(seg * seg))
            seg_min = float(np.min(seg))
            seg_max = float(np.max(seg))
            if seg_min < self._acc_min:
                self._acc_min = seg_min
            if seg_max > self._acc_max:
                self._acc_max = seg_max
            self._acc_clipped += int(
                np.count_nonzero(np.abs(seg) >= _CLIP_THRESHOLD))
            pos += take
            if self._acc_samples >= _BUCKET_SAMPLES:
                self._flush_bucket(generation, route_text, occupied)

    # ---------- gap ----------

    def mark_gap(self, reason: str) -> None:
        """显式采集间断标记（主循环在 mic 暂停/输入错误/EOF 等边界调用）。"""
        self._append_gap(reason, self._capture_generation,
                         self._last_route, self._last_occupied)

    def _append_gap(self, reason, generation, route, occupied) -> None:
        self._audio_seq += 1  # 序号从 1 起：初始 cursor=0 恰好覆盖第一条
        self._audio_frames.append(AudioFrame(
            seq=self._audio_seq, t_mono=time.monotonic(), ts=time.time(),
            capture_generation=generation, route=route, tts_occupied=occupied,
            kind="gap", reason=reason))
        self._reset_acc()

    # ---------- 文本事件 ----------

    def add_event(self, kind: str, text: str, *, capture_generation=None,
                  utterance_id=None, turn_id=None, operation_owner=None) -> None:
        """记录一条文本事件（VOICE_LOG 镜像与 U3 显式事件的统一入口）。"""
        if self._privacy_on:
            return
        text = str(text)
        self._event_seq += 1  # 序号从 1 起：初始 cursor=0 恰好覆盖第一条
        self._events.append(TelemetryEvent(
            seq=self._event_seq, instance_id=self.instance_id,
            ts=time.time(), t_mono=time.monotonic(),
            capture_generation=capture_generation, kind=kind, text=text,
            utterance_id=utterance_id, turn_id=turn_id,
            operation_owner=operation_owner))
        self._text_bytes += len(text.encode("utf-8"))
        self._trim_events()

    #: 语义别名：与 ``on_audio`` 同面的"事件写入口"（隐私拒收说明所指）
    on_event = add_event

    def clear_events(self) -> None:
        """清空文本事件环（VOICE_LOG.clear 的镜像；音频环不受影响）。"""
        self._events.clear()
        self._text_bytes = 0

    def _trim_events(self) -> None:
        while self._events and (len(self._events) > _MAX_TEXT_EVENTS
                                or self._text_bytes > _MAX_TEXT_BYTES):
            evicted = self._events.popleft()
            self._text_bytes -= len(evicted.text.encode("utf-8"))

    # ---------- 隐私联锁（AE5） ----------

    def apply_privacy(self, enabled: bool) -> None:
        """隐私开启即清空两环并拒收后续写入；关闭解除拒收。"""
        if enabled:
            self._privacy_on = True
            self._audio_frames.clear()
            self._events.clear()
            self._text_bytes = 0
            self._reset_acc()
            self._append_gap("privacy_on", self._capture_generation,
                             self._last_route, self._last_occupied)
        else:
            self._privacy_on = False

    # ---------- 游标读取面（U4 observe API 消费） ----------

    def audio_since(self, cursor: int, *, instance_id: str | None = None):
        """返回 (cursor 之后的音频帧, 下一游标, reset)，语义见模块 docstring。"""
        return self._since(self._audio_frames, self._audio_seq, cursor, instance_id)

    def events_since(self, cursor: int, *, instance_id: str | None = None):
        """返回 (cursor 之后的事件, 下一游标, reset)，语义见模块 docstring。"""
        return self._since(self._events, self._event_seq, cursor, instance_id)

    def _since(self, ring, next_seq, cursor, instance_id):
        if instance_id is not None and instance_id != self.instance_id:
            return list(ring), next_seq, True
        if cursor < 0 or cursor > next_seq:
            return list(ring), next_seq, True
        oldest = ring[0].seq if ring else next_seq
        if cursor + 1 < oldest:
            # 请求区间已有条目被淘汰/清空（含隐私清空后的 gap 辨认）
            return list(ring), next_seq, True
        return [item for item in ring if item.seq > cursor], next_seq, False

    # ---------- 内部 ----------

    def _flush_bucket(self, generation, route, occupied) -> None:
        if not self._acc_samples:
            return
        rms = math.sqrt(self._acc_energy / self._acc_samples)
        self._audio_seq += 1
        self._audio_frames.append(AudioFrame(
            seq=self._audio_seq, t_mono=time.monotonic(), ts=time.time(),
            capture_generation=generation, route=route, tts_occupied=occupied,
            kind="envelope", reason=None,
            min=self._acc_min, max=self._acc_max, rms=rms,
            peak=max(abs(self._acc_min), abs(self._acc_max)),
            clip_ratio=self._acc_clipped / self._acc_samples))
        self._reset_acc()

    def _reset_acc(self) -> None:
        self._acc_samples = 0
        self._acc_energy = 0.0
        self._acc_min = math.inf
        self._acc_max = -math.inf
        self._acc_clipped = 0
