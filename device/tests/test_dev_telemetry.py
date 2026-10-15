"""DevTelemetry 单元测试（U2 有界结构化遥测与轮次基础）。

覆盖计划 U2 测试场景（docs/plans/2026-10-15-0923-feat-developer-console-mindmap-plan.md）：
  1 隐私联锁（AE5）：开启清空两环并拒收新写入，关闭恢复；
  2 游标：增量语义、游标过旧 / 实例不匹配 / 越界 reset；
  3 已知波形：正弦/方波桶摘要直接对应输入（非 100ms 对齐 chunk 仍按样本数划界）；
  4 淘汰：音频 100 桶、文本 500 条 / 512KiB 双界；
  5 观察者：0 观察者跳过波形计算，1 观察者恢复；
  6 gap：generation 变化自动标记 + mark_gap 可读原因；
  7 主路径无感：RouteDispatch 带/不带 telemetry 行为一致；
  8 观测不碰麦克风：观察者循环不触发 mic_pause、不建第二条流；
  9 镜像：VOICE_LOG 带 sink 镜像进遥测文本环，不带 sink 行为不变。

断言全部走公开读取面（audio_since/events_since/observer_count），不摸私有属性。
"""

from __future__ import annotations

import numpy as np
import pytest

from lira.dev_telemetry import DevTelemetry
from lira.dialog import AudioRoute, Router
from lira.dialog.route_dispatch import RouteDispatch
from lira.voice_log import VOICE_LOG, VoiceLog
from test_voice_log import APPLIANCES, _AsrStub, _KwsStub

_SAMPLE_RATE = 16000
_BUCKET_SAMPLES = 1600  # 100ms @ 16kHz（与 DevTelemetry 内部约定一致）


@pytest.fixture(autouse=True)
def _clean_voice_log_singleton():
    """单例 VOICE_LOG 复位 + 镜像 sink 撤销（main 装配测试会注册，防跨文件污染）。"""
    VOICE_LOG.register_sink(None)
    VOICE_LOG.clear()
    yield
    VOICE_LOG.register_sink(None)
    VOICE_LOG.clear()


# ---------- 辅助 ----------


def _sine(seconds: float, amp: float = 0.5, freq: float = 50.0) -> np.ndarray:
    t = np.arange(int(_SAMPLE_RATE * seconds), dtype=np.float32) / _SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _feed(tel, samples, *, chunk=1600, generation=0, route=AudioRoute.WAKE,
          occupied=False) -> None:
    """按 chunk 切块喂入（模拟主循环非对齐 chunk）。"""
    for start in range(0, len(samples), chunk):
        tel.on_audio(samples[start:start + chunk], generation=generation,
                     route=route, tts_occupied=occupied)


def _envelopes(tel):
    return [f for f in tel.audio_since(0)[0] if f.kind == "envelope"]


def _gaps(tel):
    return [f for f in tel.audio_since(0)[0] if f.kind == "gap"]


def _events(tel):
    return tel.events_since(0)[0]


# ---------- 1 隐私联锁（AE5） ----------


class TestPrivacyInterlock:
    async def test_privacy_on_clears_rings_and_rejects_new_writes(self):
        tel = DevTelemetry()
        tel.add_observer()
        _feed(tel, _sine(0.5))
        tel.add_event("wake", "收到语音唤醒")
        assert _envelopes(tel) and _events(tel)

        tel.apply_privacy(True)

        assert _envelopes(tel) == []
        assert _events(tel) == []
        gaps = _gaps(tel)
        assert len(gaps) == 1 and gaps[0].reason == "privacy_on"

        # 拒收：隐私开启期间迟到的采集/识别结果无新写入（结构性双保险）
        _feed(tel, _sine(0.5))
        tel.add_event("asr", "迟到的识别文本")
        assert _envelopes(tel) == []
        assert _events(tel) == []

        # 迟到的代次变化同样零写入：帧数不变（含 gap 在内任何帧都不落）
        frames_before = len(tel.audio_since(0)[0])
        _feed(tel, _sine(0.5), generation=7)
        assert len(tel.audio_since(0)[0]) == frames_before

        # 隐私解除后同代次继续喂：不再落 generation_changed gap——证明代次
        # 在隐私期间已被静默跟踪（恢复不产生虚假代次边界）；包络恢复计算
        tel.apply_privacy(False)
        _feed(tel, _sine(0.5), generation=7)
        gaps = _gaps(tel)
        assert len(gaps) == 1 and gaps[0].reason == "privacy_on"
        assert _envelopes(tel)

    async def test_privacy_off_resumes_accepting_writes(self):
        tel = DevTelemetry()
        tel.add_observer()
        tel.apply_privacy(True)
        tel.apply_privacy(False)
        _feed(tel, _sine(0.2))
        tel.add_event("wake", "恢复后事件")
        assert len(_envelopes(tel)) == 2
        assert [e.text for e in _events(tel)] == ["恢复后事件"]

    async def test_privacy_rejects_mirror_sink_writes_only(self):
        tel = DevTelemetry()
        ring = VoiceLog()
        ring.register_sink(tel)
        tel.apply_privacy(True)
        ring.log("asr", "隐私期间的识别文本")
        # 老端点数据面照常，遥测侧拒收镜像
        assert [e.text for e in ring.snapshot()] == ["隐私期间的识别文本"]
        assert _events(tel) == []


# ---------- 2 游标 ----------


class TestCursorSemantics:
    async def test_instance_id_changes_per_construction(self):
        """进程重启 = 新实例：前端凭 instance_id 辨认识别 reset。"""
        a, b = DevTelemetry(), DevTelemetry()
        assert a.instance_id and b.instance_id
        assert a.instance_id != b.instance_id

    async def test_empty_ring_fresh_cursor_is_not_reset(self):
        tel = DevTelemetry()
        assert tel.audio_since(0) == ([], 0, False)
        assert tel.events_since(0) == ([], 0, False)

    async def test_audio_cursor_incremental(self):
        tel = DevTelemetry()
        tel.add_observer()
        _feed(tel, _sine(0.3))  # 3 桶
        items, next_seq, reset = tel.audio_since(0)
        assert [f.seq for f in items] == [1, 2, 3]
        assert next_seq == 3 and reset is False

        items, next_seq, reset = tel.audio_since(next_seq)
        assert items == [] and next_seq == 3 and reset is False

        _feed(tel, _sine(0.1))
        items, next_seq, reset = tel.audio_since(3)
        assert [f.seq for f in items] == [4] and reset is False

    async def test_event_cursor_incremental(self):
        tel = DevTelemetry()
        tel.add_event("wake", "一")
        tel.add_event("asr", "二")
        items, next_seq, reset = tel.events_since(0)
        assert [e.seq for e in items] == [1, 2]
        assert next_seq == 2 and reset is False
        items, _, _ = tel.events_since(1)
        assert [e.seq for e in items] == [2]

    async def test_stale_cursor_returns_reset_with_full_window(self):
        tel = DevTelemetry()
        tel.add_observer()
        _feed(tel, _sine(10.2))  # 102 桶 > 环上限：最早 2 桶（seq 1/2）已淘汰
        items, next_seq, reset = tel.audio_since(0)
        assert reset is True
        assert [f.seq for f in items] == list(range(3, 103))
        assert next_seq == 102

    async def test_instance_mismatch_returns_reset(self):
        tel = DevTelemetry()
        tel.add_event("wake", "x")
        items, next_seq, reset = tel.events_since(0, instance_id="dead-instance")
        assert reset is True and len(items) == 1
        assert next_seq == 1

    async def test_future_cursor_returns_reset_with_window(self):
        tel = DevTelemetry()
        tel.add_event("wake", "x")
        items, _, reset = tel.events_since(99)
        assert reset is True and len(items) == 1


# ---------- 3 已知波形 ----------


class TestWaveformBuckets:
    async def test_sine_bucket_stats_match_input(self):
        tel = DevTelemetry()
        tel.add_observer()
        amp, freq = 0.5, 50.0
        samples = _sine(0.5, amp=amp, freq=freq)  # 8000 样本 -> 5 桶
        _feed(tel, samples)
        frames = _envelopes(tel)
        assert len(frames) == 5
        for i, f in enumerate(frames):
            seg = samples[i * _BUCKET_SAMPLES:(i + 1) * _BUCKET_SAMPLES]
            assert f.min == pytest.approx(float(np.min(seg)), abs=1e-6)
            assert f.max == pytest.approx(float(np.max(seg)), abs=1e-6)
            assert f.peak == pytest.approx(amp, abs=1e-3)
            assert f.rms == pytest.approx(amp / np.sqrt(2), rel=0.01)
            assert f.clip_ratio == 0.0
            assert f.capture_generation == 0
            assert f.route == "wake"
            assert f.tts_occupied is False

    async def test_square_wave_clip_ratio(self):
        tel = DevTelemetry()
        tel.add_observer()
        samples = np.concatenate(
            [np.full(400, 1.0, np.float32), np.full(1200, 0.1, np.float32)])
        _feed(tel, samples)
        f = _envelopes(tel)[0]
        assert f.min == pytest.approx(0.1)
        assert f.max == pytest.approx(1.0)
        assert f.peak == pytest.approx(1.0)
        assert f.clip_ratio == pytest.approx(0.25)

    async def test_non_aligned_chunk_boundaries_follow_sample_count(self):
        """chunk 非 100ms 对齐（700 样本/块）时桶边界仍按 1600 样本划分。"""
        tel = DevTelemetry()
        tel.add_observer()
        samples = _sine(0.5)  # 8000 样本
        for start in range(0, len(samples), 700):
            tel.on_audio(samples[start:start + 700], generation=0,
                         route=AudioRoute.WAKE, tts_occupied=False)
        frames = _envelopes(tel)
        assert len(frames) == 5
        for i, f in enumerate(frames):
            seg = samples[i * _BUCKET_SAMPLES:(i + 1) * _BUCKET_SAMPLES]
            assert f.min == pytest.approx(float(np.min(seg)), abs=1e-6)
            assert f.max == pytest.approx(float(np.max(seg)), abs=1e-6)
            assert f.rms == pytest.approx(
                float(np.sqrt(np.mean(seg * seg))), rel=1e-6)

    async def test_generation_change_flushes_partial_bucket_then_gap(self):
        tel = DevTelemetry()
        tel.add_observer()
        tel.on_audio(np.full(1000, 0.2, np.float32), generation=0,
                     route=AudioRoute.WAKE, tts_occupied=False)
        tel.on_audio(np.full(1000, 0.4, np.float32), generation=1,
                     route=AudioRoute.LISTEN, tts_occupied=False)
        items, _, _ = tel.audio_since(0)
        assert [f.kind for f in items] == ["envelope", "gap"]
        first = items[0]
        # 旧代次的未满桶被闭合，不混入新代次样本
        assert first.capture_generation == 0
        assert first.min == pytest.approx(0.2) and first.max == pytest.approx(0.2)
        assert items[1].reason == "generation_changed"
        assert items[1].capture_generation == 1

        tel.on_audio(np.full(1000, 0.4, np.float32), generation=1,
                     route=AudioRoute.LISTEN, tts_occupied=False)
        frames = _envelopes(tel)
        assert len(frames) == 2
        assert frames[-1].capture_generation == 1
        assert frames[-1].min == pytest.approx(0.4)


# ---------- 4 双界淘汰 ----------


class TestBoundedEviction:
    async def test_audio_ring_evicts_beyond_100_buckets(self):
        tel = DevTelemetry()
        tel.add_observer()
        _feed(tel, _sine(15.0), chunk=1600)  # 150 桶 > 100
        items, next_seq, reset = tel.audio_since(0)
        assert len(items) == 100
        assert reset is True  # 最早 50 桶已被淘汰
        assert [f.seq for f in items] == list(range(51, 151))
        assert next_seq == 150

    async def test_text_ring_evicts_beyond_500_entries(self):
        tel = DevTelemetry()
        for i in range(600):
            tel.add_event("state", f"e{i}")
        items, next_seq, reset = tel.events_since(0)
        assert [e.seq for e in items] == list(range(101, 601))
        assert next_seq == 600 and reset is True

    async def test_text_ring_evicts_on_512kib_byte_bound(self):
        tel = DevTelemetry()
        big = "x" * (64 * 1024)  # 每条 64KiB
        for _ in range(9):
            tel.add_event("llm", big)
        items = _events(tel)
        assert len(items) == 8  # 8 * 64KiB == 512KiB 恰好达界
        assert sum(len(e.text.encode("utf-8")) for e in items) <= 512 * 1024
        # 单条超大文本本身也留不下：界不被突破（整环清空）
        tel.add_event("llm", "y" * (600 * 1024))
        assert _events(tel) == []

    async def test_text_ring_mixed_bound_keeps_both_limits(self):
        tel = DevTelemetry()
        text = "字" * 200  # 每条 600 utf-8 字节
        for i in range(900):
            tel.add_event("state", f"{i}{text}")
        items = _events(tel)
        assert len(items) <= 500
        assert sum(len(e.text.encode("utf-8")) for e in items) <= 512 * 1024


# ---------- 5 观察者门控 ----------


class TestObserverGating:
    async def test_no_observer_skips_envelope_but_records_events(self):
        tel = DevTelemetry()
        assert tel.observer_count == 0
        _feed(tel, _sine(0.5))
        tel.add_event("wake", "文本事件不受观察者影响")
        assert _envelopes(tel) == []
        assert [e.text for e in _events(tel)] == ["文本事件不受观察者影响"]

    async def test_subscription_gates_waveform_computation(self):
        tel = DevTelemetry()
        _feed(tel, _sine(0.1))
        assert _envelopes(tel) == []
        tel.add_observer()
        _feed(tel, _sine(0.1))
        assert len(_envelopes(tel)) == 1  # 0->1 后下一块音频恢复计算
        tel.remove_observer()
        _feed(tel, _sine(0.1))
        assert len(_envelopes(tel)) == 1  # 移除后再停
        assert tel.observer_count == 0

    async def test_observer_count_floors_at_zero(self):
        tel = DevTelemetry()
        tel.remove_observer()
        assert tel.observer_count == 0
        tel.add_observer()
        tel.add_observer()
        tel.remove_observer()
        tel.remove_observer()
        tel.remove_observer()
        assert tel.observer_count == 0

    async def test_unobserved_samples_do_not_leak_into_next_bucket(self):
        """退订即弃未满桶：观察断档前的零散样本不混入恢复后的首个桶。"""
        tel = DevTelemetry()
        tel.add_observer()
        tel.on_audio(np.full(800, 0.2, np.float32), generation=0,
                     route=AudioRoute.WAKE, tts_occupied=False)
        tel.remove_observer()
        tel.on_audio(np.full(800, 0.2, np.float32), generation=0,
                     route=AudioRoute.WAKE, tts_occupied=False)
        tel.add_observer()
        tel.on_audio(np.full(800, 0.4, np.float32), generation=0,
                     route=AudioRoute.WAKE, tts_occupied=False)
        tel.on_audio(np.full(800, 0.4, np.float32), generation=0,
                     route=AudioRoute.WAKE, tts_occupied=False)
        frames = _envelopes(tel)
        assert len(frames) == 1
        assert frames[0].min == pytest.approx(0.4)
        assert frames[0].max == pytest.approx(0.4)

    async def test_poll_activity_window_gates_computation(self, monkeypatch):
        """页面轮询即观察（U5 接线）：note_observer_activity 续期 2s 活动窗，
        与手动计数并存；窗口过期后计算自然停止（KTD2 无观察者不计算）。"""
        tel = DevTelemetry()
        clock = [100.0]
        monkeypatch.setattr("lira.dev_telemetry.time.monotonic", lambda: clock[0])
        assert tel.observer_count == 0
        tel.note_observer_activity()
        assert tel.observer_count == 1
        _feed(tel, _sine(0.1))
        assert len(_envelopes(tel)) == 1
        clock[0] += 3.0  # 活动窗（2s）已过：停拉页面的包络计算自然停止
        assert tel.observer_count == 0
        _feed(tel, _sine(0.1))
        assert len(_envelopes(tel)) == 1
        tel.note_observer_activity()  # 手动计数与活动窗并存（任一为真即计算）
        tel.add_observer()
        _feed(tel, _sine(0.1))
        assert len(_envelopes(tel)) == 2


# ---------- 6 gap 标记 ----------


class TestGapMarkers:
    async def test_mark_gap_records_readable_reason(self):
        tel = DevTelemetry()
        tel.mark_gap("麦克风暂停：dev 录音窗口独占采集设备")
        gaps = _gaps(tel)
        assert len(gaps) == 1
        assert gaps[0].kind == "gap"
        assert gaps[0].reason == "麦克风暂停：dev 录音窗口独占采集设备"

    async def test_gap_and_buckets_share_one_audio_seq(self):
        tel = DevTelemetry()
        tel.add_observer()
        _feed(tel, _sine(0.2))
        tel.mark_gap("音频输入中断")
        _feed(tel, _sine(0.1))
        items, _, _ = tel.audio_since(0)
        assert [f.seq for f in items] == [1, 2, 3, 4]
        assert [f.kind for f in items] == ["envelope", "envelope", "gap", "envelope"]


# ---------- 7 RouteDispatch 旁路无感 ----------


class _Holder:
    """轻量 streams 桩（test_voice_log._Holder 同构 + 可选 telemetry）。"""

    def __init__(self, route, telemetry=None, **streams) -> None:
        from lira.dialog import DialogEngine

        self.route = route
        self.engine = DialogEngine(Router(APPLIANCES), _SilentCb())
        self.wake_stream = streams.get("wake")
        self.asr_stream = streams.get("asr")
        self.playback_stream = streams.get("playback")
        self.telemetry = telemetry


class _SilentCb:
    """最小回调桩（不产生 VOICE_LOG 噪声的面）。"""

    def speak(self, text: str) -> None:
        pass

    def start_listening(self) -> None:
        pass

    def set_audio_route(self, route) -> None:
        pass

    def start_capture(self):
        import asyncio

        return asyncio.ensure_future(asyncio.sleep(0))

    def send_ir(self, device: str, action: str) -> None:
        pass

    def playback_pause(self) -> None: ...
    def playback_resume(self) -> None: ...
    def playback_stop(self) -> None: ...
    def playback_read_again(self) -> None: ...
    def playback_volume_up(self) -> None: ...
    def playback_volume_down(self) -> None: ...


class TestRouteDispatchBypass:
    SAMPLES = b"\x00\x00" * 160  # 160 样本（0.01s，非 100ms 对齐）

    def _wake_dispatch(self, telemetry):
        holder = _Holder(AudioRoute.WAKE, telemetry=telemetry, wake=_KwsStub(None))
        pending: list = []
        dispatch = RouteDispatch(streams=holder, wakeword="小丽拉", spawn=pending.append)
        return dispatch, pending

    def _drain(self, pending) -> int:
        for coro in pending:
            coro.close()
        return len(pending)

    async def test_feed_with_and_without_telemetry_logged_identically(self):
        runs = []
        for telemetry in (None, DevTelemetry()):
            VOICE_LOG.clear()
            dispatch, pending = self._wake_dispatch(telemetry)
            for _ in range(100):
                dispatch.feed(self.SAMPLES)
            runs.append(([(e.kind, e.text) for e in VOICE_LOG.snapshot()],
                         self._drain(pending)))
        assert runs[0] == runs[1]
        # 既有每秒统计行为原样（与 test_voice_log 同断言面，audio kind 过滤）
        audio = [text for kind, text in runs[0][0] if kind == "audio"]
        assert len(audio) == 1 and "RMS=-200.0 dBFS" in audio[0]

    async def test_asr_delivery_identical_with_telemetry(self):
        runs = []
        for telemetry in (None, DevTelemetry()):
            VOICE_LOG.clear()
            holder = _Holder(AudioRoute.LISTEN, telemetry=telemetry,
                             asr=_AsrStub("打开台灯"))
            pending: list = []
            dispatch = RouteDispatch(streams=holder, wakeword="小丽拉",
                                     spawn=pending.append)
            dispatch.feed(self.SAMPLES)
            runs.append(([(e.kind, e.text) for e in VOICE_LOG.snapshot()],
                         self._drain(pending)))
        assert runs[0] == runs[1]
        assert any(kind == "asr" and "识别到指令：打开台灯" in text
                   for kind, text in runs[1][0])

    async def test_feed_records_envelope_with_generation_route_tts(self):
        tel = DevTelemetry()
        tel.add_observer()
        holder = _Holder(AudioRoute.LISTEN, telemetry=tel, wake=_KwsStub(None))
        dispatch = RouteDispatch(streams=holder, wakeword="小丽拉",
                                 spawn=lambda coro: coro.close())
        for _ in range(10):  # 10 * 160 = 1600 样本 -> 恰好 1 桶
            dispatch.feed(self.SAMPLES)
        frames = _envelopes(tel)
        assert len(frames) == 1
        f = frames[0]
        assert f.capture_generation == 0
        assert f.route == "listen"
        assert f.tts_occupied is False


# ---------- 8 观测不碰麦克风 ----------


class TestObservationNeverTouchesMic:
    async def test_dev_telemetry_has_no_audio_device_dependency(self):
        """DevTelemetry 是纯内存观察对象：构造/订阅/喂样本均不需音频设备。"""
        a, b = DevTelemetry(), DevTelemetry()
        a.add_observer()
        _feed(a, np.zeros(1600, np.float32))
        a.remove_observer()
        assert a.instance_id != b.instance_id

    async def test_runtime_observer_cycles_do_not_pause_mic_or_reopen_streams(self, tmp_path, monkeypatch):
        from test_main_loop import make_rig

        rig = make_rig(tmp_path)
        rt = rig.runtime
        pause_calls: list = []
        resume_calls: list = []

        async def _spy_pause():
            pause_calls.append(1)

        async def _spy_resume():
            resume_calls.append(1)

        monkeypatch.setattr(rt, "mic_pause", _spy_pause)
        monkeypatch.setattr(rt, "mic_resume", _spy_resume)
        streams_before = (rt.wake_stream, rt.asr_stream,
                          rt.playback_stream, rt.command_stream)
        telemetry = rt.telemetry

        for _ in range(3):
            telemetry.add_observer()
            _feed(telemetry, _sine(0.1))
            telemetry.mark_gap("观察循环期间的显式标记")
            telemetry.remove_observer()
        assert pause_calls == [] and resume_calls == []
        # 未构造第二个 InputStream：四条流对象原封不动
        assert (rt.wake_stream, rt.asr_stream,
                rt.playback_stream, rt.command_stream) == streams_before
        # 收尾后（0 观察者）继续喂音频不再产生新包络
        before = len(_envelopes(telemetry))
        _feed(telemetry, _sine(0.1))
        assert len(_envelopes(telemetry)) == before


# ---------- 9 VOICE_LOG 镜像 ----------


class TestVoiceLogMirror:
    async def test_voice_log_with_sink_mirrors_into_telemetry(self):
        tel = DevTelemetry()
        ring = VoiceLog()
        ring.register_sink(tel)
        ring.log("wake", "收到语音唤醒（小丽拉）")
        ring.log("asr", "识别到指令：打开台灯")
        mirrored = [e.text for e in _events(tel)]
        assert mirrored == ["收到语音唤醒（小丽拉）", "识别到指令：打开台灯"]
        # 老端点数据面不变：VOICE_LOG 自己的 deque 原样
        assert [e.text for e in ring.snapshot()] == mirrored
        ring.clear()
        assert ring.snapshot() == []
        assert _events(tel) == []  # clear 同步清 sink 文本环

    async def test_voice_log_without_sink_unchanged(self):
        ring = VoiceLog()
        ring.log("wake", "x")
        ring.clear()
        ring.log("asr", "y")
        assert [e.text for e in ring.snapshot()] == ["y"]

    async def test_runtime_wires_voice_log_sink(self, tmp_path):
        from test_main_loop import make_rig

        rig = make_rig(tmp_path)
        VOICE_LOG.clear()  # 装配期引擎初始标签已被镜像，清空后验证增量
        VOICE_LOG.log("state", "待机：持续等待唤醒中")
        events = _events(rig.runtime.telemetry)
        assert [e.text for e in events] == ["待机：持续等待唤醒中"]
        assert all(e.instance_id == rig.runtime.telemetry.instance_id
                   for e in events)
