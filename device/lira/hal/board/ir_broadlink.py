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

from lira.hal.base import HalError, IrController

__all__ = ["BroadlinkIrController", "parse_mac", "DEFAULT_LEARN_POLL_INTERVAL"]

#: 学习轮询间隔（check_data 无码即空转；过密徒增 UDP 包，过疏拖慢学习响应）
DEFAULT_LEARN_POLL_INTERVAL = 0.3


def parse_mac(mac: str) -> bytes:
    """MAC 字符串转 6 字节。接受 ``ec:0b:ae:0c:74:e4`` / 连字符 / 裸 12 位十六进制。"""
    text = mac.strip().replace(":", "").replace("-", "").lower()
    if len(text) != 12:
        raise HalError(f"ir_mac={mac!r} 非法（应为 6 字节十六进制，如 ec:0b:ae:0c:74:e4）。")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise HalError(f"ir_mac={mac!r} 含非十六进制字符。") from exc


async def _default_factory(host: str, mac: str, hello_timeout: float):
    """默认设备工厂：经 hello(ip) 获取正确设备类型（含 devtype→类映射与锁定标志）。"""
    from broadlink import hello  # 延迟导入：x86 开发/单测环境无此依赖（见模块注）

    return await hello(host, timeout=hello_timeout)


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
        device_factory: ``(host, mac, timeout) -> Device`` 协程，测试注入 fake 用；
            默认经 broadlink.hello 发现。
    """

    def __init__(
        self,
        host: str,
        mac: str,
        *,
        hello_timeout: float = 5.0,
        learn_poll_interval: float = DEFAULT_LEARN_POLL_INTERVAL,
        device_factory=None,
    ) -> None:
        self._host = host
        self._mac = mac
        self._mac_bytes = parse_mac(mac)
        self._hello_timeout = hello_timeout
        self._poll = learn_poll_interval
        self._factory = device_factory or _default_factory
        self._device = None

    # ---------- 生命周期 ----------

    async def open(self) -> None:
        """发现设备并认证。失败一律 HalError（带解锁/在线指引），不抛裸异常。"""
        try:
            device = await self._factory(self._host, self._mac, self._hello_timeout)
            # MAC 核对在控制器层（factory 注入路径同样过闸）：
            # 换设备/配置过期时 fail-closed，拒绝把红外码发进别的设备
            reported = bytes(device.mac)
            if reported != self._mac_bytes:
                raise HalError(
                    f"BroadLink 设备 MAC 不符: hello 报告 {reported.hex(':')}，"
                    f"配置为 {self._mac}。检查配置是否过期或接了别的设备。"
                )
            await device.auth()
        except HalError:
            raise
        except Exception as exc:  # 上游异常统一转 HalError（HAL 约定）
            raise _translate_open_error(exc) from exc
        self._device = device
        logging.info(
            "BroadLink IR 就绪: host=%s devtype=%s model=%s locked=%s",
            self._host, hex(device.devtype), getattr(device, "model", "?"),
            getattr(device, "is_locked", "?"),
        )

    async def close(self) -> None:
        if self._device is not None:
            aclose = getattr(self._device, "aclose", None)
            if aclose is not None:
                await aclose()
        self._device = None

    def _require_device(self):
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
        await device.send_data(payload)

    async def learn(self, timeout_seconds: float = 10.0) -> str:
        """录一帧原始码：进入学习模式后轮询，返回 base64 码值。

        指向设备遥控器按键须在超时窗口内发生；超时抛 HalError（appliances 层
        转 learn_failed 话术）。
        """
        device = self._require_device()
        await device.enter_learning()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            try:
                data = await device.check_data()
            except Exception as exc:  # noqa: BLE001
                # RM4 固件怪癖：未录到码时部分设备回 -5 StorageError 而非空载荷
                # （按"还没录到"续等；Home Assistant 集成同此处理，见
                # python-broadlink#698/#650）。其他异常照常上抛。
                if "Storage" in type(exc).__name__:
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
