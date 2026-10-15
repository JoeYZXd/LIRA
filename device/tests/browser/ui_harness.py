"""dev 控制台浏览器测试助手（U5）：真实服务句柄 + 页面操作工具。

独立模块（不叫 conftest）：pytest 的 conftest 按目录各自导入为顶层
``conftest`` 模块，直接 import 会与 tests/e2e/conftest 撞名；本模块名唯一，
测试与夹具都从这里取（与 e2e 的 harness.py 同先例）。

数据纪律：页面展示的曲线/轮卡/日志全部来自 /dev/api/observe 真实往返，
测试只经 DevTelemetry 控制柄喂入可控帧/事件，不在浏览器里伪造数据。
"""

from __future__ import annotations

import asyncio
import socket
import time
from types import SimpleNamespace

PASSPHRASE = "browser-passphrase"


class BrowserRuntime:
    """U1/U4 面：store/privacy/vault/settings/engine + **真实 DevTelemetry**。

    与 test_dev_console.FakeRuntime 同思路；telemetry 必须真实——U5 页面
    消费的是 observe 契约（游标/reset/序列化），替身无法证明契约。
    """

    def __init__(self) -> None:
        from lira.appliances.store import ApplianceStore
        from lira.dev_telemetry import DevTelemetry
        from lira.privacy import PassphraseVault, PrivacyState
        from lira.settings import DeviceSettings

        self.store = ApplianceStore(":memory:")
        self.privacy = PrivacyState()
        self.vault = PassphraseVault(self.store)
        self.settings = DeviceSettings()
        self.sync_status = {"session": "not_configured"}
        self.sync_pull_event = asyncio.Event()
        self.camera = None
        self.ocr = None
        self.recent_speaks: list[str] = []
        # dev 页仅需 engine.state.value（/dev/api/voice-log 展示面）
        self.engine = SimpleNamespace(state=SimpleNamespace(value="standby"))
        self.telemetry = DevTelemetry()
        # U7 声纹面：默认未装配（磁贴诚实降级）；用例经 attach_voice_memory
        # 挂真实 PersonalMemory + VoiceCandidates 后磁贴才有数据
        self.personal_memory = None
        self.speaker_candidates = None

    def sync_pull(self) -> str:
        return "not_configured"

    def status(self) -> dict:
        return {
            "engine": "standby",
            "route": "wake",
            "privacy_on": self.privacy.is_on,
            "sync": {"session": self.sync_status["session"],
                     "epoch": None, "version": None},
            "network": False,
            "breaker": "CLOSED",
            "models": {"audio": False, "ocr": False},
        }


class DevBrowserServer:
    """测试服务句柄：base_url + runtime/telemetry 控制柄 + 喂数助手。"""

    def __init__(self, base_url: str, runtime: BrowserRuntime, server) -> None:
        self.base_url = base_url
        self.runtime = runtime
        self._server = server

    @property
    def telemetry(self):
        """实时取 runtime.telemetry（reset 用例会整体替换实例）。"""
        return self.runtime.telemetry

    def observe_enabled(self) -> None:
        """开观察者（KTD2：无观察者不算包络；页面测试期保持订阅不退订）。"""
        self.telemetry.add_observer()

    def feed_silence(self, buckets: int = 1, *, generation: int = 0) -> None:
        """喂入 buckets 个 100ms 真静音桶（1600 样本/桶 @16kHz，rms≈0）。"""
        import numpy as np

        from lira.dialog import AudioRoute

        samples = np.zeros(1600 * buckets, dtype=np.float32)
        self.telemetry.on_audio(samples, generation=generation,
                                route=AudioRoute.WAKE, tts_occupied=False)

    def set_privacy(self, on: bool) -> None:
        """镜像 main.py 装配次序的隐私联锁（privacy.subscribe 双道同触发）。"""
        self.runtime.privacy._on = on
        self.telemetry.apply_privacy(on)


def attach_voice_memory(runtime) -> "object":
    """挂真实 PersonalMemory + VoiceCandidates（共享连接，同生产装配形态）。

    dev.py 经 getattr 在请求时解析 runtime 属性，服务无需重建：测试先 attach
    再打开磁贴即可。内存库（:memory:），用例结束随进程丢弃。
    """
    from lira.config import SpeakerConfig
    from lira.personal_memory import PersonalMemory
    from lira.speaker_candidates import VoiceCandidates

    memory = PersonalMemory(":memory:", None, "kimi-k2.6",
                            lambda: runtime.privacy.is_on)
    runtime.personal_memory = memory
    runtime.speaker_candidates = VoiceCandidates(
        memory.db, fingerprint="browser-fp:2", profiles_provider=memory.profiles,
        params=SpeakerConfig(), privacy=lambda: runtime.privacy.is_on)
    return memory


def vc_samples(seed: int, seconds: float = 4.0, level: float = 0.2):
    """过质量门的可控假音频（内容与向量无关；seed 区分去重键）。"""
    import numpy as np

    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(16000 * seconds)) * level).astype(np.float32)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- 页面操作助手（同步 Playwright API） ----------


def login_console(page, server) -> None:
    """浏览器走真实登录页表单：POST /dev/login → 会话 cookie → 控制台。"""
    page.goto(server.base_url + "/dev")
    page.wait_for_selector("#dev-login-form")
    page.fill("#pass", PASSPHRASE)
    page.click("#dev-login-form button[type=submit]")
    page.wait_for_selector("#dev-console")


def open_live_tile(page) -> None:
    """展开实时区磁贴（触达即开始 observe 节拍，KTD4 展开才订阅）。"""
    head = page.locator("#tile-voicelog .tile-head")
    if head.get_attribute("aria-expanded") != "true":
        head.click()
    page.wait_for_selector("#tile-voicelog-body:not([hidden])")


def wait_for_text(page, selector: str, substring: str, timeout: int = 8000) -> None:
    """等待元素文本包含子串（observe 是 250ms 轮询流，轮询等待避免硬睡）。"""
    page.wait_for_function(
        "([sel, sub]) => ((document.querySelector(sel) || {}).textContent || '')"
        ".includes(sub)",
        arg=[selector, substring], timeout=timeout)


# ---------- 页面内 observe 拉取探针 ----------
#
# 为什么不用 page.route 计数/拦截：sync Playwright 的 route 回调只在测试线程
# 调用 Playwright API 时被派发（实测 time.sleep 期间事件排队不执行），计数与
# 时序断言都不可靠。改为 add_init_script 在文档起点包一层 fetch，页面自己
# 记录每次 observe 拉取的 URL 与 performance.now() 时间——断言读的是页面内
# 真实发起的请求，与事件派发时机无关。


_OBSERVE_PROBE = """
(() => {
  const entries = [];
  const orig = window.fetch;
  window.fetch = function (url, opts) {
    const u = String(url || '');
    if (u.includes('/dev/api/observe')) {
      entries.push({url: u, t: performance.now()});
    }
    return orig.apply(this, arguments);
  };
  window.__observeProbe = entries;
})();
"""


def install_observe_probe(page) -> None:
    """导航前安装：记录页面发起的每一次 /dev/api/observe 拉取。"""
    page.add_init_script(_OBSERVE_PROBE)


def observe_entries(page) -> list[dict]:
    """已发起的 observe 拉取（[{url, t}]，t 为页面内毫秒时钟）。"""
    return page.evaluate(
        "() => (window.__observeProbe || []).map(e => ({url: e.url, t: e.t}))")


def observe_count(page) -> int:
    return len(observe_entries(page))


def wait_observe_count(page, n: int, timeout_s: float = 15.0) -> list[dict]:
    """等待拉取数达到 n（用 page.wait_for_timeout 轮询，保持事件派发畅通）。"""
    deadline = time.monotonic() + timeout_s
    entries = observe_entries(page)
    while len(entries) < n and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        entries = observe_entries(page)
    assert len(entries) >= n, f"等待 observe 拉取达到 {n} 条超时，实际 {len(entries)}"
    return entries


def wait_incremental_pull(page, timeout_s: float = 8.0) -> None:
    """等待出现带游标的增量拉取（首拉基线已完成）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any("audio_cursor=" in e["url"] for e in observe_entries(page)):
            return
        page.wait_for_timeout(100)
    raise AssertionError("等待带游标的增量 observe 拉取超时")
