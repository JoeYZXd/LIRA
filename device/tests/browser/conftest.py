"""dev 控制台浏览器测试夹具（dev-console 改版 U5）。

真实服务：uvicorn 线程 + 动态端口拉起 create_app（UiServices + DevHandle），
runtime 挂**真实 DevTelemetry**，用例经句柄喂入可控帧/事件作为断言数据源。

收集纪律（对应计划 Verification Contract 分层表"浏览器为独立测试层"）：
  - 显式指定本目录（`pytest tests/browser`）时才收集执行——浏览器测试单独
    成层运行；
  - 默认全量 `pytest` 不收集：pytest-playwright 的 sync 模式会让会话主线程
    常驻一个 running event loop，pytest-asyncio 的 Runner 随后对**同进程内
    所有异步测试**抛 "Runner.run() cannot be called from a running event
    loop"（本仓 pytest 9.1.1 + pytest-asyncio 1.4.0 实测），因此两层必须
    分进程执行；
  - Playwright 为可选测试依赖（pyproject 的 browser 组，文档化依赖、环境
    预装），未安装时同样不收集，浏览器运行时不进设备生产依赖。
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

_argv = " ".join(sys.argv[1:]).replace("\\", "/")
_BROWSER_REQUESTED = "tests/browser" in _argv or "browser/" in _argv

try:
    import pytest_playwright  # noqa: F401
    import uvicorn
except ImportError:  # pragma: no cover - 浏览器依赖未装的机器上不收集
    collect_ignore = ["test_dev_console_ui.py"]
else:
    if not _BROWSER_REQUESTED:
        # 默认全量运行：与 pytest-asyncio 测试必须分进程（见模块 docstring）
        collect_ignore = ["test_dev_console_ui.py"]
    else:
        collect_ignore = []

from ui_harness import PASSPHRASE, BrowserRuntime, DevBrowserServer, free_port


@pytest.fixture()
def dev_server():
    """（真实 app + 真实 DevTelemetry）uvicorn 线程服务；teardown 稳妥停机。"""
    from lira.ui.app import UiServices, create_app
    from lira.ui.dev import DevHandle

    rt = BrowserRuntime()
    rt.vault.set(PASSPHRASE)
    handle = DevHandle(rt)
    rt.dev_handle = handle  # dev.py 的测试访问面（与 make_client 同约定）
    rt.telemetry.add_observer()
    app = create_app(UiServices(privacy=rt.privacy, vault=rt.vault,
                                store=rt.store, settings=rt.settings,
                                dev=handle))
    config = uvicorn.Config(app, host="127.0.0.1", port=free_port(),
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("uvicorn 测试服务未能启动")
    port = server.servers[0].sockets[0].getsockname()[1]
    yield DevBrowserServer(f"http://127.0.0.1:{port}", rt, server)
    server.should_exit = True
    thread.join(timeout=5)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=2)
