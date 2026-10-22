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


class TestConversationPaths:
    @pytest.mark.parametrize('width', [390, 1440])
    def test_switch_readback_and_missing_configuration(self, dev_server, page, width, tmp_path):
        from types import SimpleNamespace
        from lira.conversation_paths import ConversationPaths
        from lira.audio.doubao_realtime import RealtimeConfig
        rt = dev_server.runtime
        rt._mic_paused = False
        rt.engine.close = lambda: None
        rt.clear_voice_identity = lambda enabled: None
        rt.pipeline = SimpleNamespace(playback_stop=lambda:None)
        rt.dispatcher = SimpleNamespace(clear=lambda:None)
        async def stop(): pass
        rt.tts = SimpleNamespace(stop=stop)
        rt.conversation_paths = ConversationPaths(rt, env={})
        page.set_viewport_size({'width':width,'height':900})
        login_console(page, dev_server)
        wait_for_text(page, '#conversation-current', 'A · 现有语音链路')
        page.select_option('#conversation-path', 'doubao_realtime')
        page.click('#conversation-switch')
        wait_for_text(page, '#conversation-message', '请先配置')
        assert page.locator('#conversation-start').is_disabled()
        assert rt.conversation_paths.mode=='classic'
        rt.conversation_paths.config = RealtimeConfig(api_key='never-render-this-secret', voice='test-voice')
        page.click('#conversation-switch')
        wait_for_text(page, '#conversation-current', 'B · 豆包实时语音')
        assert page.locator('#conversation-start').is_enabled()
        assert 'never-render-this-secret' not in page.content()
        page.reload()
        wait_for_text(page, '#conversation-current', 'B · 豆包实时语音')
        assert page.locator('#conversation-path').input_value()=='doubao_realtime'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#conversation-path').focus()
        import os
        from pathlib import Path
        target = Path(os.environ.get('LIRA_SCREENSHOT_DIR',str(tmp_path)))
        target.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target / f'conversation-path-{width}.png'), full_page=True)
        page.select_option('#conversation-path','classic')
        page.click('#conversation-switch')
        wait_for_text(page, '#conversation-current', 'A · 现有语音链路')


class TestDoubaoLab:
    @pytest.mark.parametrize('width',[390,1440])
    def test_multiturn_feedback_export_and_refresh(self,dev_server,page,width,tmp_path):
        import asyncio,json,os
        import numpy as np
        from pathlib import Path
        from types import SimpleNamespace
        from lira.audio.doubao_realtime import RealtimeConfig
        from lira.ui.doubao_lab import DoubaoLab
        rt=dev_server.runtime
        async def stop():pass
        rt._mic_paused=False
        rt.tts=SimpleNamespace(sample_rate=16000,generate=lambda text,speed:np.full(1600,.1,np.float32))
        rt.conversation_paths=SimpleNamespace(config=RealtimeConfig(api_key='never-visible',voice='voice'),stop=stop,external_test=False)
        rt.conversation_paths.snapshot=lambda:{'mode':'classic','state':'standby','doubao_configured':True,
            'doubao_missing':[],'cloud_active':False,'local_enrollment':False}
        class Session:
            def __init__(self,**kwargs):self.kwargs=kwargs
            async def open(self):pass
            async def close(self):pass
            async def send(self,*a,**kw):pass
            def feed(self,samples):
                if not np.any(samples):return
                async def reply():
                    self.kwargs['on_event']('user_text','我想录入声纹')
                    await self.kwargs['on_tool']('start_voice_enrollment',{})
                    async def chunks():yield b'\0'*960
                    await self.kwargs['player'].play_stream(chunks())
                    self.kwargs['on_event']('assistant_text','本次仅模拟，没有保存声纹。')
                    self.kwargs['on_event']('response_done','')
                asyncio.create_task(reply())
        rt.dev_handle._doubao_lab=DoubaoLab(rt,rt.dev_handle.dev_mutex,session_factory=Session)
        page.set_viewport_size({'width':width,'height':960})
        login_console(page,dev_server)
        page.click('#app-tile-doubao-lab')
        page.click('#lab-start')
        wait_for_text(page,'#lab-status','已连接')
        for text in ['我想录入声纹','取消刚才的操作']:
            page.fill('#lab-text',text)
            page.click('#lab-send')
            page.wait_for_function("[...document.querySelectorAll('.lab-turn')].every(t => t.textContent.includes('回复完成')) && document.querySelectorAll('.lab-turn').length === " + str(1 if text.startswith('我') else 2))
        card=page.locator('.lab-turn').first
        card.locator('.lab-feedback').locator('..').locator('summary').click()
        card.locator('select').select_option('tool')
        card.locator('textarea').nth(0).fill('不应该把模拟说成真实执行')
        card.locator('textarea').nth(1).fill('请明确说只是模拟')
        with page.expect_download() as download:
            page.click('#lab-export-json')
        data=json.loads(Path(download.value.path()).read_text())
        assert len(data['turns'])==2
        assert data['turns'][0]['feedback']['note']=='不应该把模拟说成真实执行'
        assert data['turns'][0]['feedback']['expected']=='请明确说只是模拟'
        assert data['tool_execution']=='simulated' and 'never-visible' not in json.dumps(data)
        assert len({t['session'] for t in data['turns']})==1
        page.click('#lab-stop')
        wait_for_text(page,'#lab-status','会话已结束')
        page.reload()
        page.click('#app-tile-doubao-lab')
        page.wait_for_selector('.lab-turn')
        assert page.locator('.lab-turn').count()==2
        assert page.locator('.lab-turn textarea').first.input_value()=='不应该把模拟说成真实执行'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        target=Path(os.environ.get('LIRA_SCREENSHOT_DIR',str(tmp_path)))
        target.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target/f'doubao-lab-{width}.png'),full_page=True)
        page.evaluate("""() => {
          window.labAudioRef = document.querySelector('.lab-turn audio');
          window.labAudioPaused = 0;
          const original = window.labAudioRef.pause.bind(window.labAudioRef);
          window.labAudioRef.pause = () => { window.labAudioPaused++; original(); };
        }""")
        assert page.request.post(dev_server.base_url+'/dev/api/doubao-lab',data={'action':'clear'}).ok
        page.wait_for_function("document.querySelectorAll('.lab-turn').length === 0")
        assert page.evaluate('window.labAudioPaused > 0 && !window.labAudioRef.getAttribute("src")')

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
        for sel in ("#app-tile-voicelog", "#app-tile-overview",
                    "#app-tile-settings"):
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
        page.focus("#app-tile-voicelog")
        page.keyboard.press("Enter")
        page.wait_for_selector("#tile-voicelog-body:not([hidden])")
        assert page.locator(head).get_attribute("aria-expanded") == "true"
        page.keyboard.press("Escape")
        page.wait_for_selector("#tile-voicelog-body[hidden]", state="attached")
        assert page.locator(head).get_attribute("aria-expanded") == "false"
        assert page.evaluate("() => document.activeElement.id") == "app-tile-voicelog", \
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
        page.locator("#app-tile-voice-candidates").click()
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


class TestJevProbe:
    @pytest.mark.parametrize("width", [390, 768, 1440, 1920])
    def test_full_width_top_panel_and_unconfigured_state(self, dev_server, page, monkeypatch, width):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        page.set_viewport_size({"width": width, "height": 960})
        login_console(page, dev_server)
        wait_for_text(page, "#jev-ready", "未配置")
        assert page.locator("#tile-jev").is_visible()
        assert page.locator("#jev-start").is_disabled()
        assert page.locator("#jev-prepare").is_visible()
        assert page.locator("#tile-jev").bounding_box()["y"] < 140
        assert page.evaluate("getComputedStyle(document.querySelector('.dev-console')).display") == "flex"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.locator(".console-shell").bounding_box()["width"] == width
        if width in (390, 1440):
            page.screenshot(path=f"/mnt/c/Users/Joey/.codex/visualizations/2026/10/16/01a14212-5e29-70f3-9118-ffd855e09c09/jev-console-{width}.png", full_page=True)

    def test_typing_prediction_and_finish_play_cached_audio(self, dev_server, page):
        import asyncio
        import numpy as np
        from lira.audio.tts import PlaybackCompletion
        from lira.audio.voice_metrics import current_trace, mark

        class ControlledDecision:
            configured = True

            async def choose(self, text, phase):
                return {"choice": "cancel" if "不录" in text else "start", "confidence": .9,
                        "model": "browser-test-only", "probabilities": {}}

        class TestTts:
            occupied = False
            sample_rate = 16000
            calls = 0
            plays = 0

            def generate(self, text, speed):
                self.calls += 1
                return np.ones(160, dtype=np.float32) * .1

            def speak_cached(self, samples, *, owner, volume):
                self.plays += 1
                done = PlaybackCompletion(owner)
                trace = current_trace.get()

                async def output():
                    mark(trace, "output_consumed")
                    await asyncio.sleep(.03)
                    mark(trace, "output_drained")
                    done.finish("success")

                asyncio.create_task(output())
                return done

            async def stop(self, **kwargs):
                pass

        rt = dev_server.runtime
        rt.tts = TestTts()
        rt.dev_handle.jev_probe.decision = ControlledDecision()
        login_console(page, dev_server)
        page.locator("#jev-prepare").click()
        wait_for_text(page, "#jev-ready", "可用")
        page.locator("#jev-start").click()
        page.locator("#jev-text").fill("帮我录入声纹")
        wait_for_text(page, "#jev-decisions", "start")
        assert rt.tts.plays == 0
        page.locator("#jev-text").fill("帮我录入声纹，不过今天先不录")
        wait_for_text(page, "#jev-decisions", "cancel")
        page.locator("#jev-finish").click()
        wait_for_text(page, "#jev-message", "播放完成")
        assert "这次录入已取消" in page.locator("#jev-reply").text_content()
        assert "ms" in page.locator("#jev-total").text_content()
        assert rt.tts.plays == 1
        assert rt.tts.calls == 11
        # Cancel a scheduled snapshot before its timer fires, then start again.
        page.locator("#jev-start").click()
        page.locator("#jev-text").fill("录入")
        page.locator("#jev-cancel").click()
        wait_for_text(page, "#jev-message", "已取消")
        page.locator("#jev-start").click()
        page.locator("#jev-text").fill("帮我录入声纹")
        wait_for_text(page, "#jev-decisions", "start")
        page.locator("#jev-cancel").click()
        wait_for_text(page, "#jev-message", "已取消")
        page.locator("#jev-start").click()
        page.locator("#jev-text").fill("开始录入")
        page.locator("#app-tile-jev").click()
        assert page.locator("#tile-jev").is_hidden()
        wait_for_text(page, "#jev-message", "已取消")
        assert rt.dev_handle.jev_probe.run["state"] == "cancelled"


class TestCosts:
    @pytest.mark.parametrize('width',[390,1440])
    def test_balance_daily_breakdown_and_warning(self,dev_server,page,width):
        import httpx
        from types import SimpleNamespace
        from lira.usage import AccountBalance,UsageLedger
        rt=dev_server.runtime
        ledger=UsageLedger(rt.store)
        ledger.add('normal','sessions',2)
        ledger.add('normal','input_seconds',60)
        ledger.add('test','turns',3)
        rt.conversation_paths=SimpleNamespace(usage=ledger,snapshot=lambda:{})
        rt.dev_handle.account_balance=AccountBalance(env={
            'VOLCENGINE_ACCESS_KEY_ID':'test-ak','VOLCENGINE_SECRET_ACCESS_KEY':'test-sk'},
            transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'Result':{
                'AvailableBalance':'12.50','CashBalance':'12.50','ArrearsBalance':'0'}})))
        page.set_viewport_size({'width':width,'height':1000})
        login_console(page,dev_server)
        page.locator('#app-tile-costs').click()
        wait_for_text(page,'#cost-balance','12.50')
        wait_for_text(page,'#cost-input','1.00')
        assert page.locator('#cost-days tr').count()==14
        wait_for_text(page,'#cost-warning','低于')
        assert page.locator('#cost-details').get_attribute('open') is None
        assert not page.locator('#cost-days').is_visible()
        page.locator('#cost-details > summary').click()
        page.locator('#cost-threshold').fill('5')
        assert page.locator('#cost-warning').inner_text()==''
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=f'/mnt/c/Users/Joey/.codex/visualizations/2026/10/16/01a14212-5e29-70f3-9118-ffd855e09c09/costs-{width}.png',full_page=True)


class TestAppCenter:
    @pytest.mark.parametrize("width", [390, 768, 1440, 1920])
    def test_multiple_windows_and_adaptive_layout(self, dev_server, page, width):
        page.set_viewport_size({"width": width, "height": 1000})
        login_console(page, dev_server)
        assert page.locator(".app-launch").count() == 13

        page.locator("#app-tile-overview").click()
        assert page.locator("#tile-overview").is_visible()
        assert page.locator("#tile-settings").is_visible()
        assert page.locator("#tile-jev").is_visible()
        wait_for_text(page, "#workspace-count", "3 个")
        assert page.locator("#app-tile-overview").get_attribute("aria-pressed") == "true"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        left = page.locator(".app-center").bounding_box()
        right = page.locator("#workspace").bounding_box()
        assert right["x"] >= left["x"] + left["width"]
        panels = page.locator("#workspace > .tile:not([hidden])")
        positions = [round(panels.nth(i).bounding_box()["x"]) for i in range(panels.count())]
        if width >= 1440:
            assert len(set(positions)) >= 2
        else:
            assert len(set(positions)) == 1
        if width in (390, 1440):
            page.screenshot(path=f"/mnt/c/Users/Joey/.codex/visualizations/2026/10/16/01a14212-5e29-70f3-9118-ffd855e09c09/app-center-{width}.png", full_page=True)

    def test_close_reopen_preserves_fields_and_empty_workspace(self, dev_server, page):
        login_console(page, dev_server)
        page.locator("#app-tile-voice").click()
        page.locator("#tts-text").fill("保留输入，不执行播报")
        page.locator("#tile-voice .tile-head").click()
        assert page.locator("#tile-voice").is_hidden()
        assert page.locator("#app-tile-voice").get_attribute("aria-pressed") == "false"
        page.locator("#app-tile-voice").click()
        assert page.locator("#tts-text").input_value() == "保留输入，不执行播报"
        ids = page.locator('.app-launch[aria-pressed="true"]').evaluate_all("nodes => nodes.map(n => n.id)")
        for app_id in ids:
            page.locator("#" + app_id).click()
        assert page.locator("#workspace-empty").is_visible()
        wait_for_text(page, "#workspace-count", "0 个")
        page.locator("#app-tile-settings").click()
        assert page.locator("#tile-settings").is_visible()
        assert page.locator("#workspace-empty").is_hidden()


class TestJevRegionFailure:
    def test_region_error_is_visible_and_stops_this_run(self, dev_server, page):
        from types import SimpleNamespace
        import numpy as np
        from lira.ui.jev_probe import CUES, ProbeError

        class RegionDenied:
            configured = True
            calls = 0

            async def choose(self, text, phase):
                self.calls += 1
                raise ProbeError("TypeSafe 当前地区不可用（HTTP 451）。本轮测试已停止。", 451)

        probe = dev_server.runtime.dev_handle.jev_probe
        probe.decision = RegionDenied()
        dev_server.runtime.tts = SimpleNamespace(occupied=False)
        probe.cache = {key: np.ones(16, dtype=np.float32) for key in CUES}
        probe.cache_speed = dev_server.runtime.settings.tts_speed
        login_console(page, dev_server)
        page.locator("#jev-start").click()
        page.locator("#jev-text").fill("请录入声纹")
        wait_for_text(page, "#jev-message", "当前地区不可用")
        wait_for_text(page, "#jev-decisions", "451")
        assert page.locator("#jev-text").is_disabled()
        assert page.locator("#jev-finish").is_disabled()
        assert probe.run["state"] == "failed"
        assert probe.decision.calls == 1
        assert not probe.mutex.locked()


class TestAudioLogSummary:
    @staticmethod
    def sample(route="wake", clipped="0.0"):
        return ("收到处理后音频：RMS=-200.0 dBFS，峰值=0.000，削波=" + clipped +
                "%，路由=" + route + "，播报占用=False；唤醒 KWS 持续检测（未报告新命中）")

    def test_repeated_capture_is_compact_and_keeps_important_events(self, dev_server, page):
        login_console(page, dev_server)
        open_live_tile(page)
        dev_server.telemetry.add_event("wake", "重要唤醒事件不能被周期收音挤走")
        wait_for_text(page, "#log-view", "重要唤醒事件")
        for batch in range(6):
            for _ in range(100):
                dev_server.telemetry.add_event("audio", self.sample())
            wait_for_text(page, "#log-view", "共 " + str((batch + 1) * 100) + " 次")
        assert page.locator('#log-view [data-kind="audio"]').count() == 1
        assert page.locator('#log-view [data-kind="wake"]').count() == 1
        summary = page.locator("#log-view .vl-audio-summary")
        assert not summary.get_attribute("open")
        summary.locator("summary").click()
        assert summary.locator(".vl-audio-detail").is_visible()
        assert "RMS=-200.0" in summary.text_content()
        dev_server.telemetry.add_event("audio", self.sample())
        wait_for_text(page, "#log-view", "共 601 次")
        assert page.locator("#log-view .vl-audio-summary").get_attribute("open") is not None

    def test_state_changes_and_clipping_are_not_hidden(self, dev_server, page):
        login_console(page, dev_server)
        open_live_tile(page)
        for _ in range(3):
            dev_server.telemetry.add_event("audio", self.sample())
        dev_server.telemetry.add_event("asr", "用户请求保留逐条显示")
        dev_server.telemetry.add_event("audio", self.sample(route="asr"))
        dev_server.telemetry.add_event("audio", self.sample(route="asr", clipped="2.0"))
        wait_for_text(page, "#log-view", "削波=2.0")
        assert page.locator("#log-view .vl-audio-summary").count() == 2
        assert page.locator('#log-view [data-kind="asr"]').count() == 1
        assert page.locator('#log-view .vl-row[data-kind="audio"]').count() == 3
