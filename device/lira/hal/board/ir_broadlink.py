"""板上红外传输层（M5/U10 改道）：BroadLink RM4 Mini + python-broadlink 局域网本地协议。

2026-10-03 改道（deploy/SETUP.md 2.6）：官方内核未启用 RC_CORE，gpio-ir/ir-ctl 路线
废弃；RM4 Mini USB 供电、WiFi 入家庭局域网，python-broadlink 走 UDP 本地协议
（不经云，R9 断外网不影响）。

依赖纪律（版本锁定）：python-broadlink==1.0.6 以 --ignore-requires-python 装入板上
venv——上游 1.0.x 元数据要求 py>=3.13，实测板上 py3.11 发现/认证全链路正常；安装
步骤与锁定理由见 deploy/SETUP.md 2.6。不入 pyproject：x86 开发/单测不需要此依赖，
本模块延迟导入，测试经 device_factory 注入 fake 设备。

安全：本层只做传输（发/录原始码）；enabled/高危确认等业务闸在 appliances 层
（IRService 双层 fail-closed），HAL 不做业务判断（hal/base.py 约定）。

码值格式：base64(原始 pulse/space 载荷)，本地库按不透明字符串存取（appliances 层
不解释内容；换传输层无需迁移历史码值之外的数据）。

命令行：`python -m lira.hal.board.ir_broadlink` = 局域网发现探测（打印 ip/mac/
devtype/锁定状态并试认证），供 SETUP 2.6 配置信息采集用。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from lira.hal.mock.ir import MockIrController
from lira.hal.base import HalError, IrController, parse_mac  # parse_mac: 再导出（配置/CLI 用）

__all__ = ["BroadlinkIrController", "parse_mac", "DEFAULT_LEARN_POLL_INTERVAL"]

#: 学习轮询间隔（check_data 无码即空转；过密徒增 UDP 包，过疏拖慢学习响应）
DEFAULT_LEARN_POLL_INTERVAL = 0.3


async def _default_factory(host: str, hello_timeout: float) -> Any:
    """默认设备工厂：经 hello(ip) 获取正确设备类型（含 devtype→类映射与锁定标志）。

    返回值是 broadlink.device.Device（依赖仅在板上 venv，x86 不装，故注解 Any）。
    """
    from broadlink import hello  # 延迟导入：x86 开发/单测环境无此依赖（见模块注）

    return await hello(host, timeout=hello_timeout)


async def _aclose_quietly(device: Any) -> None:
    """尽力释放已认证设备（1.0.6 Device 持有 asyncio UDP transport，弃置即泄漏）。

    仅用于 open 失败路径：清理失败只记 debug，绝不掩盖正在上抛的原始错误。
    """
    aclose = getattr(device, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # noqa: BLE001 - 清理失败不掩盖原始错误
        logging.debug("BroadLink 设备释放失败（忽略）", exc_info=True)


def _translate_open_error(exc: Exception) -> HalError:
    """连接/认证异常 → 带修复指引的 HalError（按上游异常类名分流，避免依赖其导入）。"""
    name = type(exc).__name__
    if "Authentication" in name:
        return HalError(
            "BroadLink 认证被拒（-1 常见于设备锁定到云账户）：官方 App → 设备 → "
            "设置 → 关闭「安全锁/Lock Device」后重试。"
        )
    if "Timeout" in name:
        return HalError(f"BroadLink 设备不在线或网络不可达: {exc!r}")
    return HalError(f"BroadLink 连接/认证失败: {exc!r}")


class BroadlinkIrController(IrController):
    """RM4 Mini 红外控制器：hello 发现 → auth 建会话 → send_data / enter_learning。

    Attributes:
        device_factory: ``(host, timeout) -> Device`` 协程，测试注入 fake 用；
            默认经 broadlink.hello 发现（MAC 核对由 open() 在返回后执行）。
    """

    #: R9：open 失败（RM4 离线/锁定）降级 Mock——家电控制不可用但主循环不崩
    fallback_factory = MockIrController

    def __init__(
        self,
        host: str,
        mac: str,
        *,
        hello_timeout: float = 5.0,
        learn_poll_interval: float = DEFAULT_LEARN_POLL_INTERVAL,
        device_factory: Callable[[str, float], Awaitable[Any]] | None = None,
    ) -> None:
        self._host = host
        self._mac = mac
        self._mac_bytes = parse_mac(mac)
        self._hello_timeout = hello_timeout
        self._poll = learn_poll_interval
        self._factory = device_factory or _default_factory
        self._device: Any = None

    # ---------- 生命周期 ----------

    async def open(self) -> None:
        """发现设备并认证。失败一律 HalError（带解锁/在线指引），不抛裸异常。"""
        device: Any = None
        try:
            device = await self._factory(self._host, self._hello_timeout)
            await device.auth()
        except Exception as exc:  # 上游异常统一转 HalError（HAL 约定）
            if device is not None:
                # 认证失败时 device 已持有 UDP transport，弃置即泄漏——尽力释放
                await _aclose_quietly(device)
            raise _translate_open_error(exc) from exc
        # MAC 核对（factory 注入路径同样过闸）：换设备/配置过期时 fail-closed，
        # 拒绝把红外码发进别的设备
        reported = bytes(device.mac)
        if reported != self._mac_bytes:
            await _aclose_quietly(device)
            raise HalError(
                f"BroadLink 设备 MAC 不符: hello 报告 {reported.hex(':')}，"
                f"配置为 {self._mac}。检查配置是否过期或接了别的设备。"
            )
        self._device = device
        logging.info(
            "BroadLink IR 就绪: host=%s devtype=%s model=%s locked=%s",
            self._host, hex(device.devtype), getattr(device, "model", "?"),
            getattr(device, "is_locked", "?"),
        )

    async def close(self) -> None:
        """释放设备会话。幂等且不抛：关闭失败不阻断停机（含 __aexit__ 展开）。"""
        device = self._device
        if device is None:
            return
        try:
            aclose = getattr(device, "aclose", None)
            if aclose is not None:
                await aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不拖垮停机路径（日志可见）
            logging.warning("event=ir_close_error", exc_info=True)
        finally:
            self._device = None

    def _require_device(self) -> Any:
        if self._device is None:
            raise HalError("BroadLink IR 未连接（open 未成功）。")
        return self._device

    # ---------- 收发（hal.base.IrController 接口） ----------

    async def send(self, code: str) -> None:
        """回放一帧原始码（base64 载荷直发；码值合法性在此把关）。"""
        device = self._require_device()
        try:
            payload = base64.b64decode(code.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HalError(f"红外码值不是合法 base64: {exc}") from exc
        if not payload:
            raise HalError("红外码值为空，拒绝发送。")
        try:
            await device.send_data(payload)
        except Exception as exc:  # noqa: BLE001 - 库异常统一转 HalError（HAL 约定）
            raise HalError(f"BroadLink 发送失败: {exc!r}") from exc

    async def learn(self, timeout_seconds: float = 10.0) -> str:
        """录一帧原始码：进入学习模式后轮询，返回 base64 码值。

        指向设备遥控器按键须在超时窗口内发生；超时抛 HalError（appliances 层
        转 learn_failed 话术）。
        """
        device = self._require_device()
        try:
            await device.enter_learning()
        except Exception as exc:  # noqa: BLE001 - 库异常统一转 HalError（HAL 约定）
            raise HalError(f"BroadLink 进入学习模式失败: {exc!r}") from exc
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            try:
                data = await device.check_data()
            except Exception as exc:  # noqa: BLE001
                # RM4 固件怪癖：未录到码时部分设备不回空载荷而回错误码——
                # python-broadlink==1.0.6 分类：-5 StorageError / -6
                # StructureAbnormalError / -10 ReadError，均按"还没录到"续等
                # （HA 集成同此处理，python-broadlink#698/#650）。按类名匹配
                # 而非 isinstance：x86 单测注入同名 fake 即可覆盖（broadlink
                # 不装 x86），并以 errno 兜底防上游改名漂移。其他异常照常上抛。
                name = type(exc).__name__
                if ("Storage" in name or "Read" in name or "Structure" in name
                        or getattr(exc, "errno", None) in (-5, -6, -10)):
                    data = b""
                else:
                    raise
            if data:
                return base64.b64encode(bytes(data)).decode("ascii")
            if loop.time() >= deadline:
                raise HalError(
                    f"红外学习超时（{timeout_seconds}s 内未录到码值）："
                    "请确认学习期间用原遥控器对准 RM4 Mini 按键。"
                )
            await asyncio.sleep(self._poll)


async def _discover_cli() -> int:
    """发现探测：打印局域网内 Broadlink 设备并逐一试认证（SETUP 2.6 第 2 步用）。"""
    try:
        from broadlink import discover
    except ImportError:
        print("python-broadlink 未安装（板上安装命令见 deploy/SETUP.md 2.6）。", file=sys.stderr)
        return 3
    devices = await discover(timeout=5.0)
    if not devices:
        print("未发现 Broadlink 设备（确认设备已配网且与本机同网段）。")
        return 1
    rc = 0
    for d in devices:
        print(
            f"ip={d.host[0]}:{d.host[1]}",
            f"mac={bytes(d.mac).hex(':')}",
            f"devtype={hex(d.devtype)}",
            f"model={getattr(d, 'model', '?')!r}",
            f"name={getattr(d, 'name', '')!r}",
            f"is_locked={getattr(d, 'is_locked', '?')}",
        )
        try:
            await d.auth()
            print("  auth OK")
        except Exception as exc:  # noqa: BLE001 — 探测工具，如实报告
            print(f"  auth FAILED: {exc!r}")
            rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(_discover_cli()))
