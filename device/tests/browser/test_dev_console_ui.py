"""dev 控制台实时区浏览器测试（dev-console 改版 U5，Playwright + 真实 observe）。

执行方式：显式 `pytest tests/browser` 单独成层运行（独立可选依赖 pyproject
browser 组）；默认全量 pytest 不收集本目录——pytest-playwright 的 sync 会话
与 pytest-asyncio 必须分进程（原因见 conftest docstring）。

对应计划 U5 四个测试场景与 Verification（docs/plans/2026-10-15-0923-…-plan.md）：
  AE3 三态       —— 无帧"等待数据"、真静音（有数据电平≈0）、采集间断各态可辨；
                   数据停更明确标注（绘制停顿绝不伪造声音数据）；
  跨拉取归并     —— 同一 turn 的事件分两批喂入仍合并为一张轮卡（无重复）；
                   实例重启 → reset → 旧轮卡清空重建；暂停滚动时新事件仍进
                   缓冲且视图不跳滚；
  AE6            —— 390/768/1440 三视口关键控件可见可点；键盘展开/Escape 收起
                   且焦点归还；reduced-motion 下信息完整（不依赖动画）；
  生命周期       —— 页面隐藏停拉、恢复可见恰好一次首拉重建；服务端持续 503 时
                   客户端按 1/2/4/8s 退避且间隔递增、恢复后重建；未登录零观察
                   请求（无假成功）；密钥轮换后 401 跳登录；隐私 403 清空敏感
                   展示并停拉、手动恢复后重建。

数据与观察纪律：页面数据全部来自 /dev/api/observe 真实往返；测试仅经真实
DevTelemetry 喂帧/事件，不在浏览器里注入任何展示数据。拉取计数用页面内
fetch 探针（见 ui_harness 说明；不用 page.route——sync API 事件回调只在
Playwright API 调用时派发，time.sleep 期间不执行，计数/时序不可靠）。
"""

from __future__ import annotations

import time

import pytest

from ui_harness import (
    attach_voice_memory,
    install_observe_probe,
    login_console,
    observe_count,
    observe_entries,
    open_live_tile,
    vc_samples,
    wait_for_text,
    wait_incremental_pull,
    wait_observe_count,
)

pytest.importorskip("playwright.sync_api")

# ---------- 场景 1（AE3）：静音 / 无数据 / 间断 展示可辨，停更不伪造 ----------


class TestCurveStates:
    def test_waiting_silence_gap_distinct(self, dev_server, page):
        """无帧=等待数据；envelope rms≈0=真静音；gap=间断+原因，三者不可混淆。"""
        login_console(page, dev_server)
        open_live_tile(page)
        # 无数据：等待标注（不是"静音"——没有帧就不能声称听到了静音）
        wait_for_text(page, "#curve-status", "等待数据")
        # 真静音：有帧、电平≈0
        dev_server.feed_silence(3)
        wait_for_text(page, "#curve-status", "静音")
        page.wait_for_selector("#curve-empty", state="hidden")
        # 数据停更：明确标注（绘制停顿不延续旧曲线假装实时）
        wait_for_text(page, "#curve-status", "音频停更", timeout=10000)
        # 采集间断：gap 画间断标注并显示原因短语
        dev_server.telemetry.mark_gap("浏览器测试：采集间断")
        wait_for_text(page, "#curve-gaps", "浏览器测试：采集间断")
        wait_for_text(page, "#curve-status", "间断")
        assert "静音" not in page.locator("#curve-status").text_content()

    def test_last_update_freezes_when_data_stops(self, dev_server, page):
        """最后更新时间只在见到新帧时前进；停更期间曲线状态可见地非实时。"""
        login_console(page, dev_server)
        open_live_tile(page)
        dev_server.feed_silence(2)
        wait_for_text(page, "#curve-status", "静音")
        frozen = page.locator("#curve-last").text_content()
        page.wait_for_timeout(1200)
        assert page.locator("#curve-last").text_content() == frozen, \
            "无新帧时'最后更新'必须冻结（不得假装仍在实时更新）"
        wait_for_text(page, "#curve-status", "音频停更", timeout=10000)


# ---------- 场景 2：跨拉取归并 / reset 重建 / 暂停滚动仍进缓冲 ----------


class TestTurnMergeAndReset:
    def test_events_across_pulls_merge_into_one_card(self, dev_server, page):
        """同一 turn 的 llm_request 先、llm_done 后（分属不同拉取）→ 一张卡
        合并展示，无重复卡；首段延迟从事件 t_mono 差计算。"""
        tel = dev_server.telemetry
        tel.add_event("turn", "打开台灯", capture_generation=1,
                      utterance_id=1, turn_id=1, operation_owner="0.op1")
        tel.add_event("route", "remote_llm", turn_id=1)
        login_console(page, dev_server)
        open_live_tile(page)
        wait_for_text(page, "#turns-view", "打开台灯")
        wait_for_text(page, "#turns-view", "remote_llm")
        # 第二批（下一拍才可见）：模型请求 → 首段 → 完成回复
        tel.add_event("llm_request", "path=chat chars=12", turn_id=1)
        page.wait_for_timeout(600)  # 确保两批事件落在不同拉取窗
        tel.add_event("llm_first_delta", "好的", turn_id=1)
        tel.add_event("llm_done", "好的，台灯已打开。", turn_id=1)
        wait_for_text(page, "#turns-view", "好的，台灯已打开。")
        assert page.locator("#turns-view .turn-card").count() == 1, \
            "跨拉取事件必须归并进同一张轮卡，不得出现重复卡"
        wait_for_text(page, "#turns-view", "ms")  # 首段延迟由 t_mono 差计算
        # 播报结束事件落地：轮卡补全"语句→播报结束"总耗时（同轮可追踪）
        tel.add_event("speak_done", "好的，台灯已打开。", turn_id=1,
                      utterance_id=1, operation_owner="0.op1")
        wait_for_text(page, "#turns-view", "播报完成")
        # 同一轮在日志区可按类别回看：语音事件 tab 有轮次/路由，模型 tab 有回复
        page.click("#tab-voice")
        wait_for_text(page, "#log-view", "打开台灯")
        page.click("#tab-model")
        wait_for_text(page, "#log-view", "好的，台灯已打开。")
        # 无 turn_id 的模型事件（如阅读白话）归"未关联轮次"，不丢弃
        tel.add_event("llm_done", "这是阅读白话的回复")
        wait_for_text(page, "#unlinked-view", "这是阅读白话的回复")

    def test_reset_clears_previous_turns_and_rebuilds(self, dev_server, page):
        """实例重启（新 instance_id）→ 服务端 reset → 旧轮卡必须清空不残留，
        新实例数据可整体重建。"""
        from lira.dev_telemetry import DevTelemetry

        dev_server.telemetry.add_event("turn", "上一轮文本", turn_id=1,
                                       utterance_id=1)
        dev_server.telemetry.add_event("llm_done", "上一轮回复", turn_id=1)
        login_console(page, dev_server)
        open_live_tile(page)
        wait_for_text(page, "#turns-view", "上一轮文本")
        # 模拟重启：替换 telemetry（新 instance_id），页面下一次拉取必须 reset
        dev_server.runtime.telemetry = DevTelemetry()
        dev_server.observe_enabled()
        page.wait_for_function(
            "() => !document.querySelector('#turns-view').textContent"
            ".includes('上一轮文本')", timeout=8000)
        assert page.locator("#turns-view .turn-card").count() == 0
        wait_for_text(page, "#turns-view", "暂无语音轮次")
        # 新实例数据可正常重建（reset 后恢复订阅不卡死）
        dev_server.telemetry.add_event("turn", "新一轮文本", turn_id=1,
                                       utterance_id=1)
        wait_for_text(page, "#turns-view", "新一轮文本")

    def test_pause_scroll_buffers_without_jump(self, dev_server, page):
        """暂停滚动（默认开）时新事件仍进展示缓冲、视图不跳滚；取消暂停且
        已滚到底时贴底跟随。"""
        login_console(page, dev_server)
        open_live_tile(page)
        assert page.locator("#log-pause").is_checked(), "暂停滚动必须默认开启"
        for i in range(30):
            dev_server.telemetry.add_event("state", f"缓冲事件{i:02d}")
        wait_for_text(page, "#log-out", "30 条缓冲")
        wait_for_text(page, "#log-view", "缓冲事件29")
        assert page.evaluate(
            "() => document.getElementById('log-view').scrollTop") == 0, \
            "暂停滚动时视图不得自动跳滚"
        # 取消暂停 + 用户已滚到底 → 新事件到达时贴底跟随
        page.uncheck("#log-pause")
        page.evaluate(
            "() => { const v = document.getElementById('log-view');"
            " v.scrollTop = v.scrollHeight; }")
        for i in range(5):
            dev_server.telemetry.add_event("state", f"跟随事件{i}")
        wait_for_text(page, "#log-view", "跟随事件4")
        bottom_gap = page.evaluate(
            "() => { const v = document.getElementById('log-view');"
            " return v.scrollHeight - v.scrollTop - v.clientHeight; }")
        assert bottom_gap <= 8, "跟随时应贴底（自动滚动仅在已滚到底时生效）"


# ---------- 场景 3（AE6）：三视口 / 键盘 / reduced-motion 信息等价 ----------


class TestResponsiveAccessibility:
    @pytest.mark.parametrize("width", [390, 768, 1440])
    def test_key_controls_visible_and_operable(self, dev_server, page, width):
        page.set_viewport_size({"width": width, "height": 800})
        login_console(page, dev_server)
        for sel in ("#tile-voicelog .tile-head", "#tile-overview .tile-head",
                    "#tile-settings .tile-head"):
            assert page.locator(sel).is_visible(), f"{width}px: {sel}"
        open_live_tile(page)
        for sel in ("#curve-canvas", "#curve-status", "#curve-last",
                    "#log-pause", "#log-filter", "#log-kind",
                    ".log-tab"):
            assert page.locator(sel).first.is_visible(), f"{width}px: {sel}"
        # 操作等价：tab 可点、系统日志 tab 切出刷新按钮
        page.click("#tab-model")
        assert page.locator("#tab-model").get_attribute("aria-selected") == "true"
        page.click("#tab-sys")
        assert page.locator("#log-refresh-btn").is_visible()

    def test_keyboard_expand_escape_focus_return(self, dev_server, page):
        login_console(page, dev_server)
        head = "#tile-voicelog .tile-head"
        page.focus(head)
        page.keyboard.press("Enter")
        page.wait_for_selector("#tile-voicelog-body:not([hidden])")
        assert page.locator(head).get_attribute("aria-expanded") == "true"
        page.keyboard.press("Escape")
        page.wait_for_selector("#tile-voicelog-body[hidden]", state="attached")
        assert page.locator(head).get_attribute("aria-expanded") == "false"
        assert page.evaluate("() => document.activeElement.className") == "tile-head", \
            "Escape 收起后焦点必须归还磁贴头"

    def test_reduced_motion_info_complete(self, dev_server, page):
        """reduced-motion 下信息不依赖动画：关键信息节点存在且可读。"""
        page.emulate_media(reduced_motion="reduce")
        login_console(page, dev_server)
        open_live_tile(page)
        dev_server.feed_silence(2)
        wait_for_text(page, "#curve-status", "静音")
        for sel in ("#curve-canvas", "#curve-status", "#curve-last",
                    "#curve-legend", "#turns-view", "#log-view"):
            assert page.locator(sel).is_visible()
        assert "最后更新" in page.locator("#curve-last").text_content()


# ---------- 场景 4：生命周期（隐藏 / 断连退避 / 未登录 / 401 / 403） ----------


class TestLifecycle:
    def test_hidden_page_stops_polling_and_rebaselines_on_visible(
            self, dev_server, page):
        install_observe_probe(page)
        login_console(page, dev_server)
        open_live_tile(page)
        wait_observe_count(page, 2)
        wait_incremental_pull(page)  # 首拉基线已完成、增量游标已持有
        before = observe_entries(page)
        # 页面隐藏（覆写可见性并广播事件）：节拍必须停
        page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState',
                {configurable: true, get: () => 'hidden'});
            Object.defineProperty(document, 'hidden',
                {configurable: true, get: () => true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.wait_for_timeout(500)  # 让在途请求自然落地
        frozen = observe_count(page)
        page.wait_for_timeout(1600)
        assert observe_count(page) == frozen, "页面隐藏后不得再发起 observe 拉取"
        # 恢复可见：恰好一次首拉重建基线（防 10 秒窗外的洞）
        page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState',
                {configurable: true, get: () => 'visible'});
            Object.defineProperty(document, 'hidden',
                {configurable: true, get: () => false});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        deadline = time.monotonic() + 8
        rebaselines: list[str] = []
        while time.monotonic() < deadline:
            rebaselines = [e["url"] for e in observe_entries(page)[len(before):]
                           if "audio_cursor=" not in e["url"]]
            if rebaselines:
                break
            page.wait_for_timeout(100)
        assert len(rebaselines) == 1, \
            f"恢复可见应恰好一次首拉重建，实际 {len(rebaselines)}"

    def test_server_503_backs_off_then_recovers(self, dev_server, page):
        """断连退避：服务端持续 503 → 1/2/4/8s 指数退避且间隔递增、状态行
        可见"已断连"；恢复后拉取重建、静音数据可见。"""
        install_observe_probe(page)
        dev_server.feed_silence(2)
        login_console(page, dev_server)
        open_live_tile(page)
        wait_incremental_pull(page)
        # 服务端观察面不可用（observe 端点 503：遥测未装配）
        real_telemetry = dev_server.runtime.telemetry
        n0 = observe_count(page)
        dev_server.runtime.telemetry = None
        try:
            wait_for_text(page, "#curve-status", "已断连")
            entries = wait_observe_count(page, n0 + 3, timeout_s=25)
        finally:
            dev_server.runtime.telemetry = real_telemetry
        # 拉取间隔按退避档位递增（禁用后前 3 拉：~立即 → +1s → +2s）。
        # 上限放宽（U8 测试评审）：定时器只会晚不会早，负载 CI 的调度抖动
        # 不应误报；递增方向是本用例的强断言。
        ts = [e["t"] for e in entries[n0:n0 + 3]]
        gap1 = (ts[1] - ts[0]) / 1000
        gap2 = (ts[2] - ts[1]) / 1000
        assert 0.8 <= gap1 <= 4.0, f"第一档退避应约 1s，实际 {gap1 / 1000:.2f}s"
        assert 1.7 <= gap2 <= 6.0, f"第二档退避应约 2s，实际 {gap2 / 1000:.2f}s"
        assert gap2 > gap1, "失败退避必须递增"
        # 恢复后：新静音帧可见，状态回到实时语义（不再断连）
        dev_server.feed_silence(2)
        wait_for_text(page, "#curve-status", "静音", timeout=15000)
        assert "已断连" not in page.locator("#curve-status").text_content()

    def test_unauthenticated_opens_login_without_observe(self, dev_server, page):
        """未登录：落登录页且零观察请求（断线/未登录状态没有假成功）。"""
        install_observe_probe(page)
        page.goto(dev_server.base_url + "/dev")
        page.wait_for_selector("#dev-login-form")
        assert page.locator("#dev-console").count() == 0
        page.wait_for_timeout(1500)
        assert observe_count(page) == 0, "未鉴权不得发起任何观察拉取"

    def test_session_invalidation_redirects_to_login(self, dev_server, page):
        """会话失效（密钥轮换，与改口令同路径）→ 下一拍 401 → 跳登录页。"""
        from lira.ui.dev import reset_dev_session_secret

        install_observe_probe(page)
        login_console(page, dev_server)
        open_live_tile(page)
        wait_observe_count(page, 2)
        reset_dev_session_secret(dev_server.runtime.store)
        page.wait_for_selector("#dev-login-form", timeout=15000)
        assert page.locator("#dev-console").count() == 0

    def test_privacy_403_clears_display_stops_and_manual_resume(
            self, dev_server, page):
        """隐私开启：清空敏感展示 + 停拉（不自动重试）；关闭后手动恢复可重建。"""
        install_observe_probe(page)
        tel = dev_server.telemetry
        tel.add_event("turn", "隐私前的语句", turn_id=1, utterance_id=1)
        tel.add_event("llm_done", "隐私前的回复", turn_id=1)
        login_console(page, dev_server)
        open_live_tile(page)
        wait_for_text(page, "#turns-view", "隐私前的语句")
        wait_observe_count(page, 2)
        dev_server.set_privacy(True)  # 服务端 403 + 遥测联锁双道（同装配次序）
        wait_for_text(page, "#privacy-banner", "隐私")
        page.wait_for_function(
            "() => !document.querySelector('#turns-view').textContent"
            ".includes('隐私前的语句')", timeout=8000)
        assert page.locator("#turns-view .turn-card").count() == 0, \
            "403 后敏感轮卡必须清空"
        frozen = observe_count(page)
        page.wait_for_timeout(1500)
        assert observe_count(page) == frozen, "隐私停拉后不得自动重试"
        # 关闭隐私 + 手动恢复入口（"重新连接"）：重新首拉回到实时观察
        dev_server.set_privacy(False)
        dev_server.feed_silence(2)
        page.click("#privacy-banner button")
        wait_for_text(page, "#curve-status", "静音", timeout=10000)
        assert not page.locator("#privacy-banner").is_visible()


# ---------- 日志区：系统日志 tab 手动刷新与清空 ----------


class TestSystemLogTab:
    def test_manual_refresh_and_clear(self, dev_server, page):
        import logging as logging_mod

        from lira.main import ensure_ring_handler

        ensure_ring_handler()
        login_console(page, dev_server)
        open_live_tile(page)
        page.click("#tab-sys")
        assert page.locator("#log-refresh-btn").is_visible()
        logging_mod.getLogger("lira.test").info("browser-syslog-marker")
        page.click("#log-refresh-btn")
        wait_for_text(page, "#log-view", "browser-syslog-marker")
        # 清空当前展示：仅清前端缓冲（服务端环不动，可再次刷新取回）
        page.get_by_role("button", name="清空当前展示").click()
        wait_for_text(page, "#log-view", "暂无匹配事件")
        page.click("#log-refresh-btn")
        wait_for_text(page, "#log-view", "browser-syslog-marker")


# ---------- U7：声纹小档案磁贴（真实 VoiceCandidates 数据源，低频单拉） ----------


def open_voice_tile(page) -> None:
    """展开声纹磁贴（触达即单拉 /dev/api/voice-candidates）。"""
    head = page.locator("#tile-voice-candidates .tile-head")
    if head.get_attribute("aria-expanded") != "true":
        head.click()
    page.wait_for_selector("#tile-voice-candidates-body:not([hidden])")


class TestVoiceCandidatesTile:
    def test_delete_candidate_confirms_and_refreshes_without_deleting_members(self, dev_server, page):
        import numpy as np
        memory = attach_voice_memory(dev_server.runtime)
        store = dev_server.runtime.speaker_candidates
        member = memory.enroll('保留成员', np.array([0.,1.],np.float32))
        cid=store.observe(np.array([1.,0.],np.float32),vc_samples(94)).candidate_id
        login_console(page, dev_server)
        open_voice_tile(page)
        button=page.get_by_role('button',name='删除未完成声纹')
        page.once('dialog',lambda dialog:dialog.dismiss())
        button.click()
        assert button.is_visible() and store._candidate_row(cid) is not None
        page.once('dialog',lambda dialog:dialog.accept())
        button.click()
        wait_for_text(page,'#voice-candidates-out','未完成声纹已删除')
        assert page.get_by_role('button',name='删除未完成声纹').count()==0
        assert store._candidate_row(cid) is None
        assert memory.people.get_member(member) and len(memory.profiles())==1

    def test_summary_members_and_progress_basis(self, dev_server, page):
        """磁贴摘要行 + 已建立成员卡 + 候选卡（状态徽标与可解释进度依据）。
        AE2：就绪 100% 但未确认的候选不计入已建立人数。"""
        import numpy as np

        memory = attach_voice_memory(dev_server.runtime)
        store = dev_server.runtime.speaker_candidates
        mama = memory.enroll("妈妈", np.array([0.0, 1.0], np.float32))
        assert mama is not None
        vec_a = np.array([1.0, 0.0], np.float32)
        store.observe(vec_a, vc_samples(11))
        store.observe(vec_a, vc_samples(12))
        store.observe(vec_a, vc_samples(13))  # 就绪（3 段 12s，一致性过）
        login_console(page, dev_server)
        open_voice_tile(page)
        wait_for_text(page, "#voice-candidates-summary", "已建立 1")
        wait_for_text(page, "#voice-candidates-summary", "待命名 1")
        wait_for_text(page, "#voice-candidates-view", "妈妈")
        wait_for_text(page, "#voice-candidates-view", "声纹已建立")
        wait_for_text(page, "#voice-candidates-view", "采集就绪待命名")
        wait_for_text(page, "#voice-candidates-view", "3/3 段")
        wait_for_text(page, "#voice-candidates-view", "一致性通过")
        summary = page.locator("#voice-candidates-summary").text_content()
        assert "已建立 1" in summary and "采集中 0" in summary

    def test_awaiting_console_confirm_posts_and_refreshes(self, dev_server, page):
        """同名待确认卡 → 点击确认 → POST 成功 → 局部刷新为已建立（AE4 路径）。"""
        import numpy as np

        memory = attach_voice_memory(dev_server.runtime)
        store = dev_server.runtime.speaker_candidates
        baba = memory.people.create_member("爸爸")["id"]
        vec_a = np.array([1.0, 0.0], np.float32)
        v1 = store.observe(vec_a, vc_samples(21))
        store.create_association_request(v1.candidate_id, baba)
        login_console(page, dev_server)
        open_voice_tile(page)
        wait_for_text(page, "#voice-candidates-view", "同名待确认")
        wait_for_text(page, "#voice-candidates-view", "爸爸")
        page.click("#voice-candidates-view button:has-text('确认关联')")
        wait_for_text(page, "#voice-candidates-view", "已建立")
        wait_for_text(page, "#voice-candidates-summary", "已建立 1")
        assert "同名待确认 0" in page.locator("#voice-candidates-summary").text_content()
        assert len(memory.profiles()) == 1, "确认真实落声纹（统一事务）"

    def test_confirm_conflict_shows_reason_in_place(self, dev_server, page):
        """409 冲突：成员已有声纹 → 卡片内显示冲突原因，不覆盖、不假成功。"""
        import numpy as np

        memory = attach_voice_memory(dev_server.runtime)
        store = dev_server.runtime.speaker_candidates
        baba = memory.enroll("爸爸", np.array([0.0, 1.0], np.float32))
        vec_a = np.array([1.0, 0.0], np.float32)
        v1 = store.observe(vec_a, vc_samples(31))
        store.create_association_request(v1.candidate_id, baba)
        login_console(page, dev_server)
        open_voice_tile(page)
        wait_for_text(page, "#voice-candidates-view", "同名待确认")
        page.click("#voice-candidates-view button:has-text('确认关联')")
        wait_for_text(page, "#voice-candidates-view", "该成员已有声纹记录")
        assert "同名待确认 1" in page.locator("#voice-candidates-summary").text_content()
        assert len(memory.profiles()) == 1, "既有声纹未被覆盖"

    def test_tile_degrades_honestly_when_not_assembled(self, dev_server, page):
        """未装配（503）→ 磁贴显示降级文案，不显示空态假象。"""
        login_console(page, dev_server)
        open_voice_tile(page)
        wait_for_text(page, "#voice-candidates-view", "声纹候选未装配")
        wait_for_text(page, "#voice-candidates-summary", "未装配")
