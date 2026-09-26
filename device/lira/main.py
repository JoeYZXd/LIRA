"""LIRA 设备端 asyncio 入口：装配各模块。

Verification（U1）::

    cd device && .venv/bin/python -m lira.main --dry-run

以全 mock HAL 装配，打印模块装配图后退出。不带 --dry-run 时进入主循环
（当前 U1 阶段主循环仅保持 HAL 上下文存活，等待 Ctrl-C；后续单元在此
挂载音频管线与对话状态机）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from lira.config import AppConfig, ConfigError, load_config
from lira.hal.mock import IMAGE_SUFFIXES, MockAudioIO, MockButton, MockCamera, MockDisplay, MockIrController


def _count_mock_images(cfg: AppConfig) -> int:
    """dry-run 不打开 HAL，这里直接数目录里的图片文件。"""
    d = cfg.hal.mock_images_dir
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def build_mock_hal(cfg: AppConfig) -> dict[str, object]:
    """按配置实例化全 mock HAL（x86 开发路径）。板上路径在 U10 接入 hal.board。"""
    if cfg.hal.backend != "mock":
        raise ConfigError(
            f"hal.backend={cfg.hal.backend!r} 的板上实现尚未实现（U10）。"
            "x86 开发请使用 hal.backend=mock。"
        )
    camera = MockCamera(cfg.hal.mock_images_dir)
    audio = MockAudioIO(cfg.hal.mock_audio_dir / "sample_16k.wav")
    ir = MockIrController()
    button = MockButton()
    display = MockDisplay()
    return {"camera": camera, "audio": audio, "ir": ir, "button": button, "display": display}


def print_assembly(cfg: AppConfig, hal: dict[str, object]) -> None:
    """打印模块装配图（dry-run 的核心产出）。"""
    print("=" * 62)
    print("LIRA 模块装配图 (dry-run)")
    print("=" * 62)
    print(f"  HAL backend : {cfg.hal.backend}")
    print(f"    camera    : {type(hal['camera']).__name__}"
          f" <- {cfg.hal.mock_images_dir}"
          f" ({_count_mock_images(cfg)} 张样张)")
    print(f"    audio     : {type(hal['audio']).__name__}"
          f" <- {hal['audio'].wav_path}")  # type: ignore[attr-defined]
    print(f"    ir        : {type(hal['ir']).__name__} (内存记录 sent_codes)")
    print(f"    button    : {type(hal['button']).__name__} (程序化 press)")
    print(f"    display   : {type(hal['display']).__name__} (内存记录 shown)")
    print(f"  LLM         : {cfg.llm.base_url}  model={cfg.llm.model}"
          f"  timeout={cfg.llm.timeout_seconds}s")
    api_key_state = "已配置" if cfg.llm.api_key else "<未设置（dry-run 允许）>"
    print(f"    api_key   : {api_key_state}")
    print(f"  log level   : {cfg.log_level}")
    print("  dialog/vision/audio 管线: 后续单元挂载 (U2-U6)")
    print("=" * 62)


async def _run_forever(cfg: AppConfig, hal: dict[str, object]) -> None:
    """进入全部 HAL 上下文并保持运行，直到收到退出信号。"""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # 非 POSIX（如 Windows）退化处理
            pass

    stack = asyncio.AsyncExitStack()
    try:
        for name, resource in hal.items():
            await stack.enter_async_context(resource)  # type: ignore[arg-type]
            logging.info("HAL 资源已就绪: %s", name)
        print("LIRA 运行中（Ctrl-C 退出）...")
        await stop.wait()
    finally:
        await stack.aclose()
        print("LIRA 已退出，HAL 资源已释放。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lira", description="LIRA 设备端主程序")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认 LIRA_CONFIG 环境变量或 device/config.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="装配全部模块（mock HAL），打印装配图后退出；不要求 api_key",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config, require_api_key=not args.dry_run)
        hal = build_mock_hal(cfg)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print_assembly(cfg, hal)

    if args.dry_run:
        print("dry-run 完成：装配校验通过，未进入主循环。")
        return 0

    try:
        asyncio.run(_run_forever(cfg, hal))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
