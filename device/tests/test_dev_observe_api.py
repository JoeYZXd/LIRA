"""dev 观察快照与设置能力接口测试（dev-console 改版 U4，KTD4）。

覆盖计划 U4 测试场景（docs/plans/2026-10-15-0923-feat-developer-console-mindmap-plan.md）：
  1 鉴权矩阵：未登录 / 伪造 cookie / 过期 cookie / 改口令后旧 cookie 一律 401；
    会话先于隐私（未鉴权 + 隐私 → 401 而非 403）；控制台关闭两路由不挂载；
  2 基础增量：首拉整窗 + reset 基线；带 next_cursor 只回新增；多标签游标互不干扰；
  3 reset：实例不匹配、游标落入已淘汰区间、越界游标、非法游标（绝不 500）；
  4 AE5：隐私开启 403 且响应无 items（接口面第二道闸，缓冲不经旁路泄漏）；
    隐私期迟到写入被联锁拒收，恢复后只有 privacy_on gap 而非旧内容；
  5 上限：单响应事件 ≤100 条且 next_cursor 可续读至读完、无重复无遗漏；
  6 设置能力：真实当前值 / 可写性 / 生效方式 / 约束，与 /dev/api/status 同源
    互证；只读项不可写；POST → 405（不存在通用参数写端点）。

复用 test_dev_console 的 make_client/login 范式与 FakeRuntime（追加真实
DevTelemetry，对齐 DeviceRuntime 装配面）。
"""

from __future__ import annotations

import numpy as np

from lira.dialog import AudioRoute
from lira.dev_telemetry import DevTelemetry
from lira.ui.dev import SESSION_COOKIE, reset_dev_session_secret

from test_dev_console import (
    PASSPHRASE,
    FakeRuntime,
    login,
    make_client,
    make_vision_client,
)

# ---------- 替身与助手 ----------


class TelemetryRuntime(FakeRuntime):
    """FakeRuntime + 结构化遥测（对齐 DeviceRuntime：runtime.telemetry）。"""

    def __init__(self) -> None:
        super().__init__()
        self.telemetry = DevTelemetry()


def make_observe_client():
    """（app, client, runtime）：挂真实 DevTelemetry 的观察面测试环境。"""
    rt = TelemetryRuntime()
    app, client, _rt = make_client(runtime=rt)
    return app, client, rt


def _observe_enabled(rt) -> None:
    """开观察者（KTD2：无观察者不计算包络；测试期不退订）。"""
    rt.telemetry.add_observer()


def _feed_audio(rt, buckets: int, *, generation: int = 0) -> None:
    """一次喂入 buckets 个 100ms 静音桶（1600 样本/桶 @16kHz）。"""
    samples = np.zeros(1600 * buckets, dtype=np.float32)
    rt.telemetry.on_audio(samples, generation=generation,
                          route=AudioRoute.WAKE, tts_occupied=False)


def _set_privacy(rt, on: bool) -> None:
    """镜像 main.py 装配的 privacy.subscribe(telemetry.apply_privacy) 联锁
    （FakeRuntime 未走装配订阅，测试显式同序触发两道）。"""
    rt.privacy._on = on
    rt.telemetry.apply_privacy(on)


def _pull(client, *, audio_cursor=None, event_cursor=None, instance=None) -> dict:
    params: dict = {}
    if audio_cursor is not None:
        params["audio_cursor"] = audio_cursor
    if event_cursor is not None:
        params["event_cursor"] = event_cursor
    if instance is not None:
        params["instance"] = instance
    r = client.get("/dev/api/observe", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 1 鉴权矩阵 ----------


class TestObserveAuth:
    def test_unauthenticated_401_both(self):
        _app, client, _rt = make_observe_client()
        for path in ("/dev/api/observe", "/dev/api/settings"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 401, path
            assert "error" in r.json()

    def test_forged_cookie_401_both(self):
        _app, client, _rt = make_observe_client()
        client.cookies.set(SESSION_COOKIE, "forged-token")
        for path in ("/dev/api/observe", "/dev/api/settings"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 401, path

    def test_expired_cookie_401_both(self, monkeypatch):
        """会话过期路径：TTL 收紧为负 → 既有 cookie 一律按过期拒（401）。"""
        import lira.ui.dev as dev_module

        _app, client, _rt = make_observe_client()
        login(client)
        assert client.get("/dev/api/settings").status_code == 200
        monkeypatch.setattr(dev_module, "_SESSION_TTL_SECONDS", -1)
        assert client.get("/dev/api/observe").status_code == 401
        assert client.get("/dev/api/settings").status_code == 401

    def test_passphrase_rotation_invalidates_old_cookie(self):
        """改口令 → 签名密钥轮换 → 旧 cookie 读新接口同样 401（AE5）。"""
        _app, client, rt = make_observe_client()
        login(client)
        assert client.get("/dev/api/settings").status_code == 200
        reset_dev_session_secret(rt.store)
        assert client.get("/dev/api/observe").status_code == 401
        assert client.get("/dev/api/settings").status_code == 401

    def test_session_checked_before_privacy(self):
        """边界次序固定：未鉴权 + 隐私开启 → 401（会话先行，非 403）。"""
        _app, client, rt = make_observe_client()
        _set_privacy(rt, True)
        r = client.get("/dev/api/observe", follow_redirects=False)
        assert r.status_code == 401

    def test_disabled_console_mounts_nothing(self):
        """控制台关闭 → 两路由不存在（404，路由表也不含）。"""
        app, client, _rt = make_client(enabled=False)
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/dev/api/observe" not in paths
        assert "/dev/api/settings" not in paths
        assert client.get("/dev/api/observe").status_code == 404
        assert client.get("/dev/api/settings").status_code == 404


# ---------- 2 基础增量 ----------


class TestObserveIncremental:
    def test_first_pull_full_window_with_reset_baseline(self):
        _app, client, rt = make_observe_client()
        login(client)
        tel = rt.telemetry
        _observe_enabled(rt)
        _feed_audio(rt, 2)
        tel.mark_gap("音频输入中断")
        tel.add_event("wake", "收到语音唤醒")
        tel.add_event("asr", "识别到指令：打开台灯",
                      capture_generation=1, utterance_id=7, turn_id=3,
                      operation_owner="0.op1")

        body = _pull(client)

        assert body["instance_id"] == tel.instance_id
        audio, events = body["audio"], body["events"]
        # 首拉 = 整窗 + reset=True（客户端据此整体建基线）
        assert audio["reset"] is True and events["reset"] is True
        assert [f["seq"] for f in audio["items"]] == [1, 2, 3]
        assert audio["next_cursor"] == 3
        gap = audio["items"][2]
        assert gap["kind"] == "gap" and gap["reason"] == "音频输入中断"
        env = audio["items"][0]
        assert env["kind"] == "envelope"
        assert env["min"] == 0.0 and env["max"] == 0.0 and env["rms"] == 0.0
        assert env["route"] == "wake" and env["tts_occupied"] is False
        assert env["capture_generation"] == 0
        assert [e["seq"] for e in events["items"]] == [1, 2]
        assert events["next_cursor"] == 2
        ev = events["items"][1]
        assert ev["text"] == "识别到指令：打开台灯"
        assert ev["kind"] == "asr" and ev["instance_id"] == tel.instance_id
        assert ev["utterance_id"] == 7 and ev["turn_id"] == 3
        assert ev["operation_owner"] == "0.op1"
        assert events["items"][0]["utterance_id"] is None, "关联字段缺省保留 null"
        # status 与 /dev/api/status 同构（KTD4：聚合快照含最新状态）
        assert set(body["status"]) >= {
            "engine", "route", "privacy_on", "sync", "sync_detail",
            "volume", "tts_speed", "loglevel", "volume_range", "speed_range",
        }

    def test_incremental_pull_after_cursor_returns_only_new(self):
        _app, client, rt = make_observe_client()
        login(client)
        tel = rt.telemetry
        _observe_enabled(rt)
        _feed_audio(rt, 2)
        tel.add_event("wake", "一")

        first = _pull(client)
        audio_cur, event_cur = (first["audio"]["next_cursor"],
                                first["events"]["next_cursor"])
        # 无新数据：空窗、不 reset、游标不动
        body = _pull(client, audio_cursor=audio_cur, event_cursor=event_cur)
        assert body["audio"]["items"] == []
        assert body["events"]["items"] == []
        assert body["audio"]["reset"] is False
        assert body["events"]["reset"] is False
        assert body["audio"]["next_cursor"] == audio_cur

        # 新增一桶一事件：只回新增
        _feed_audio(rt, 1)
        tel.add_event("state", "状态迁移")
        body = _pull(client, audio_cursor=audio_cur, event_cursor=event_cur)
        assert [f["seq"] for f in body["audio"]["items"]] == [3]
        assert [e["text"] for e in body["events"]["items"]] == ["状态迁移"]
        assert body["audio"]["next_cursor"] == 3
        assert body["events"]["next_cursor"] == 2

    def test_multi_tab_cursors_independent(self):
        """多标签：各自持游标交叉拉取，互不推进、互不干扰。"""
        _app, client, rt = make_observe_client()
        login(client)
        tel = rt.telemetry
        _observe_enabled(rt)
        _feed_audio(rt, 2)
        tel.add_event("wake", "早")

        tab_a0 = _pull(client)  # 标签 A 先打开（基线 2 桶 / 1 事件）
        _feed_audio(rt, 2)
        tel.add_event("wake", "晚")
        tab_b0 = _pull(client)  # 标签 B 稍后才打开 → 整窗（4 桶 / 2 事件）
        assert len(tab_b0["audio"]["items"]) == 4
        assert len(tab_b0["events"]["items"]) == 2
        assert tab_b0["audio"]["reset"] is True

        a_cur = (tab_a0["audio"]["next_cursor"], tab_a0["events"]["next_cursor"])
        tab_a1 = _pull(client, audio_cursor=a_cur[0], event_cursor=a_cur[1])
        # A 只补增量，B 的打开没有"消费"掉 A 的数据
        assert [f["seq"] for f in tab_a1["audio"]["items"]] == [3, 4]
        assert [e["text"] for e in tab_a1["events"]["items"]] == ["晚"]

        _feed_audio(rt, 1)
        tel.add_event("asr", "新")
        a2 = _pull(client,
                   audio_cursor=tab_a1["audio"]["next_cursor"],
                   event_cursor=tab_a1["events"]["next_cursor"])
        b1 = _pull(client,
                   audio_cursor=tab_b0["audio"]["next_cursor"],
                   event_cursor=tab_b0["events"]["next_cursor"])
        assert [f["seq"] for f in a2["audio"]["items"]] == [5]
        assert [e["text"] for e in a2["events"]["items"]] == ["新"]
        # A 读走的增量 B 仍能凭自己的游标读到：服务端环不被"消费"
        assert [f["seq"] for f in b1["audio"]["items"]] == [5]
        assert [e["text"] for e in b1["events"]["items"]] == ["新"]


# ---------- 3 reset（可恢复） ----------


class TestObserveReset:
    def test_instance_mismatch_returns_reset_with_full_window(self):
        _app, client, rt = make_observe_client()
        login(client)
        _observe_enabled(rt)
        _feed_audio(rt, 1)
        rt.telemetry.add_event("wake", "x")
        body = _pull(client, instance="dead-instance")
        assert body["audio"]["reset"] is True
        assert body["events"]["reset"] is True
        assert len(body["audio"]["items"]) == 1
        assert len(body["events"]["items"]) == 1

    def test_evicted_region_returns_reset_with_window(self):
        """游标回退到已淘汰区间（>100 帧后用旧游标）→ reset + 整窗。"""
        _app, client, rt = make_observe_client()
        login(client)
        _observe_enabled(rt)
        _feed_audio(rt, 2)
        stale = _pull(client)["audio"]["next_cursor"]  # = 2
        _feed_audio(rt, 150)  # 总 seq 152；环只留最近 100（seq 53..152）
        body = _pull(client, audio_cursor=stale)
        audio = body["audio"]
        assert audio["reset"] is True
        assert len(audio["items"]) == 100
        assert audio["items"][0]["seq"] == 53
        assert audio["next_cursor"] == 152

    def test_future_cursor_returns_reset(self):
        _app, client, rt = make_observe_client()
        login(client)
        rt.telemetry.add_event("wake", "x")
        body = _pull(client, event_cursor=99999)
        assert body["events"]["reset"] is True
        assert len(body["events"]["items"]) == 1

    def test_illegal_cursor_treated_as_first_pull_never_500(self):
        """非法游标（非整数/负数）→ 按首拉语义整窗 + reset，可恢复。"""
        _app, client, rt = make_observe_client()
        login(client)
        rt.telemetry.add_event("wake", "一")
        rt.telemetry.add_event("asr", "二")
        for bad in ("abc", "-1", "1.5"):
            r = client.get("/dev/api/observe",
                           params={"audio_cursor": bad, "event_cursor": bad})
            assert r.status_code == 200, bad
            body = r.json()
            assert body["audio"]["reset"] is True, bad
            assert body["events"]["reset"] is True, bad
            assert len(body["events"]["items"]) == 2, bad


# ---------- 4 AE5：隐私联锁 ----------


class TestObservePrivacy:
    def test_privacy_on_403_without_content_and_late_writes_dropped(self):
        """隐私开启：403 无 items（缓冲不经旁路泄漏）；迟到写入零落；
        恢复后只有 privacy_on gap，旧内容不复现。"""
        _app, client, rt = make_observe_client()
        login(client)
        tel = rt.telemetry
        _observe_enabled(rt)
        _feed_audio(rt, 2)
        tel.add_event("asr", "隐私前的识别文本")
        first = _pull(client)
        audio_cur, event_cur = (first["audio"]["next_cursor"],
                                first["events"]["next_cursor"])

        _set_privacy(rt, True)
        r = client.get("/dev/api/observe",
                       params={"audio_cursor": audio_cur,
                               "event_cursor": event_cur})
        assert r.status_code == 403
        assert set(r.json()) == {"error"}, "403 响应不得含任何 items 字段"
        # 无游标的首拉同样被闸住，不给基线快照
        assert client.get("/dev/api/observe").status_code == 403

        # 隐私期迟到写入：遥测联锁拒收（结构性双保险的"再拒收一次"）
        _feed_audio(rt, 3)
        tel.add_event("asr", "隐私期的迟到识别")

        _set_privacy(rt, False)
        body = _pull(client, audio_cursor=audio_cur, event_cursor=event_cur)
        kinds = [f["kind"] for f in body["audio"]["items"]]
        assert kinds == ["gap"], "隐私清空只能以 gap 辨认，不得有旧包络"
        assert body["audio"]["items"][0]["reason"] == "privacy_on"
        assert body["events"]["items"] == [], "恢复后事件环为空，非旧内容"
        body_all = " ".join(
            f["reason"] or "" for f in body["audio"]["items"]
        ) + " ".join(e["text"] for e in body["events"]["items"])
        assert "隐私前的识别文本" not in body_all
        assert "隐私期的迟到识别" not in body_all


# ---------- 5 单响应上限 ----------


class TestObserveEventPageCap:
    def test_events_capped_at_100_and_paging_completes(self):
        _app, client, rt = make_observe_client()
        login(client)
        tel = rt.telemetry
        for i in range(250):
            tel.add_event("state", f"事件{i}")

        body = _pull(client)
        events = body["events"]
        assert len(events["items"]) == 100, "单响应限流"
        assert events["items"][-1]["seq"] == 100
        assert events["next_cursor"] == 100, "只推进到最后返回那条"
        collected = [e["seq"] for e in events["items"]]

        cursor = events["next_cursor"]
        while True:
            body = _pull(client, event_cursor=cursor)
            items = body["events"]["items"]
            if not items:
                assert body["events"]["reset"] is False
                break
            assert len(items) <= 100
            assert body["events"]["next_cursor"] == items[-1]["seq"]
            collected.extend(e["seq"] for e in items)
            cursor = body["events"]["next_cursor"]
        assert collected == list(range(1, 251)), "续读至读完，无重复无遗漏"


# ---------- 6 设置能力元信息 ----------


def _settings_map(client) -> dict:
    body = client.get("/dev/api/settings")
    assert body.status_code == 200, body.text
    return {s["key"]: s for s in body.json()["settings"]}


class TestSettingsCapability:
    def test_values_match_runtime_and_status(self):
        """真实当前值 + 约束/生效方式元信息；与 /dev/api/status 同源互证。"""
        from lira.settings import DeviceSettings

        _app, client, rt = make_observe_client()
        login(client)
        rt.settings.set_volume(1.3)
        rt.settings.set_tts_speed(0.8)
        items = _settings_map(client)

        assert items["volume"]["value"] == 1.3
        assert items["volume"]["writable"] is True
        assert items["volume"]["effective"] == "immediate"
        assert items["volume"]["write_endpoint"] == "/volume"
        assert items["volume"]["constraint"] == {
            "type": "range",
            "min": DeviceSettings.VOLUME_RANGE[0],
            "max": DeviceSettings.VOLUME_RANGE[1],
        }
        assert items["tts_speed"]["value"] == 0.8
        assert items["tts_speed"]["write_endpoint"] == "/speed"
        assert items["tts_speed"]["constraint"]["min"] == DeviceSettings.SPEED_RANGE[0]

        status = client.get("/dev/api/status").json()
        assert items["volume"]["value"] == status["volume"]
        assert items["tts_speed"]["value"] == status["tts_speed"]
        assert items["loglevel"]["value"] == status["loglevel"]
        assert items["loglevel"]["constraint"]["values"] == [
            "DEBUG", "INFO", "WARNING", "ERROR",
        ]
        assert items["loglevel"]["write_endpoint"] == "/dev/api/loglevel"

    def test_loglevel_follows_runtime_change(self):
        import logging as logging_mod

        _app, client, _rt = make_observe_client()
        login(client)
        assert client.post("/dev/api/loglevel", json={"level": "debug"}).status_code == 200
        assert _settings_map(client)["loglevel"]["value"] == "DEBUG"
        client.post("/dev/api/loglevel", json={"level": "info"})
        assert _settings_map(client)["loglevel"]["value"] == "INFO"
        assert logging_mod.getLogger("lira").level == logging_mod.INFO

    def test_readonly_items_expose_metadata_only(self):
        _app, client, rt = make_observe_client()
        login(client)
        items = _settings_map(client)
        assert items["models"]["writable"] is False
        assert items["models"]["value"] == {"audio": False, "ocr": False}
        assert items["models"]["write_endpoint"] is None
        assert items["models"]["constraint"] is None
        assert items["sync_session"]["writable"] is False
        assert items["sync_session"]["value"] == "not_configured"
        assert items["sync_session"]["value"] == \
            rt.status()["sync"]["session"]

    def test_camera_items_only_when_camera_present(self):
        _app, client, rt = make_observe_client()
        login(client)
        keys = set(_settings_map(client))
        assert "camera_rotation" not in keys and "camera_hflip" not in keys

        _app2, client2, rt2 = make_vision_client()
        login(client2)
        items = _settings_map(client2)
        assert items["camera_rotation"]["value"] == 0
        assert items["camera_rotation"]["writable"] is True
        assert items["camera_rotation"]["constraint"] == {
            "type": "choices", "values": [0, 90, 180, 270],
        }
        assert items["camera_rotation"]["write_endpoint"] == \
            "/dev/api/camera/orientation"
        assert items["camera_hflip"]["value"] is False
        assert items["camera_hflip"]["constraint"] == {"type": "bool"}
        # 同源互证：与 /dev/api/status 相机回读一致
        status = client2.get("/dev/api/status").json()
        assert items["camera_rotation"]["value"] == status["camera_rotation"]
        assert items["camera_hflip"]["value"] == status["camera_hflip"]

    def test_settings_is_get_only_no_generic_write(self):
        """R11：settings/observe 只读；不存在通用参数写端点，POST → 405。"""
        app, client, rt = make_observe_client()
        login(client)
        assert client.post("/dev/api/settings", json={"volume": 99}).status_code == 405
        assert client.post("/dev/api/observe", json={"volume": 99}).status_code == 405
        settings_routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/dev/api/settings"
        ]
        assert len(settings_routes) == 1
        assert settings_routes[0].methods == {"GET"}
        # 405 意味着处理器从未执行：值不被"通用写入"触碰
        assert rt.settings.volume == 1.0
