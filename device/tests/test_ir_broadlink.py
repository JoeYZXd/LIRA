"""M5 板上红外传输层测试（lira/hal/board/ir_broadlink.py，fake 设备注入）。

覆盖：open 认证与 MAC 核对（happy path + 锁定/超时错误翻译）、send base64 解码
（非法码值/未连接拒发）、learn 轮询取码与超时、close 幂等、config ir_host/ir_mac
成对与格式校验。全部经 device_factory 注入 fake，不依赖 python-broadlink。

注：断言刻意钉住中文指引文案的稳定短语（指引可见性），措辞调整须同步改测试。
"""

from __future__ import annotations

import base64
import sys

import pytest

from lira.config import ConfigError, load_config
from lira.hal.base import HalError
from lira.hal.board.ir_broadlink import BroadlinkIrController, parse_mac

MAC_STR = "ec:0b:ae:0c:74:e4"
MAC_BYTES = bytes.fromhex("ec0bae0c74e4")


class FakeBroadlinkDevice:
    """行为仿 1.0.6 Device/RM4 接口子集：auth/send_data/enter_learning/check_data/aclose。"""

    def __init__(self, mac: bytes = MAC_BYTES) -> None:
        self.mac = mac
        self.devtype = 0x653A  # RM4 mini 实测 devtype
        self.model = "RM4 mini"
        self.is_locked = False
        self.auth_exc: Exception | None = None
        self.auth_calls = 0
        self.sent: list[bytes] = []
        self.enter_calls = 0
        self.check_results: list[bytes] = []  # 逐次弹出；只剩最后一个时重复返回
        self.close_calls = 0

    async def auth(self) -> bool:
        self.auth_calls += 1
        if self.auth_exc is not None:
            raise self.auth_exc
        return True

    async def send_data(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def enter_learning(self) -> None:
        self.enter_calls += 1

    async def check_data(self) -> bytes:
        if len(self.check_results) > 1:
            return self.check_results.pop(0)
        return self.check_results[0] if self.check_results else b""

    async def aclose(self) -> None:
        self.close_calls += 1


def make_factory(device: FakeBroadlinkDevice):
    """device_factory 注入桩：记录 host/timeout 传参。"""

    async def factory(host: str, timeout: float):
        device.seen_host = host
        device.seen_timeout = timeout
        return device

    return factory


def make_controller(device, **kwargs) -> BroadlinkIrController:
    kwargs.setdefault("learn_poll_interval", 0.001)
    kwargs.setdefault("hello_timeout", 0.5)
    return BroadlinkIrController("192.168.101.27", MAC_STR,
                                 device_factory=make_factory(device), **kwargs)


class _LegacyDeviceWithoutClose:
    """无 aclose 的设备替身（异常实现/旧版本）：close 必须静默容忍。"""

    mac = MAC_BYTES
    devtype = 0x653A

    async def auth(self) -> bool:
        return True


# ---------- open：发现/认证/MAC 核对 ----------


class TestOpen:
    async def test_open_auths_and_send_replays_payload(self):
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        assert device.auth_calls == 1
        assert device.seen_host == "192.168.101.27"
        payload = b"\x26\x01\x00\x9d\x03"
        await ctrl.send(base64.b64encode(payload).decode("ascii"))
        assert device.sent == [payload]
        await ctrl.close()
        assert device.close_calls == 1

    async def test_open_mac_mismatch_rejected_and_closes_device(self):
        device = FakeBroadlinkDevice(mac=bytes.fromhex("aabbccddeeff"))
        ctrl = make_controller(device)
        with pytest.raises(HalError) as ei:
            await ctrl.open()
        assert "MAC 不符" in str(ei.value)
        assert device.close_calls == 1  # 认证后的会话不弃置（防 UDP transport 泄漏）

    async def test_open_cleanup_failure_does_not_mask_primary_error(self):
        """aclose 清理自身失败时不得掩盖主错误（MAC 不符仍如实抛出）。"""
        device = FakeBroadlinkDevice(mac=bytes.fromhex("aabbccddeeff"))
        device.auth_exc = None

        async def broken_aclose() -> None:
            raise type("NetworkTimeoutError", (Exception,), {})(-1, "socket gone")

        device.aclose = broken_aclose  # type: ignore[method-assign]
        ctrl = make_controller(device)
        with pytest.raises(HalError) as ei:
            await ctrl.open()
        assert "MAC 不符" in str(ei.value)
        assert "socket gone" not in str(ei.value)

    async def test_open_auth_failure_closes_device(self):
        device = FakeBroadlinkDevice()
        device.auth_exc = type("AuthenticationError", (Exception,), {})(-1, "denied")
        ctrl = make_controller(device)
        with pytest.raises(HalError):
            await ctrl.open()
        assert device.close_calls == 1

    async def test_open_factory_failure_skips_close(self):
        """factory 本身失败（无 device 可释放）：不得误调 aclose。"""
        device = FakeBroadlinkDevice()

        async def failing_factory(host, timeout):
            raise type("NetworkTimeoutError", (Exception,), {})(-4000, "timeout")

        ctrl = BroadlinkIrController("192.168.101.27", MAC_STR,
                                     device_factory=failing_factory)
        with pytest.raises(HalError):
            await ctrl.open()
        assert device.close_calls == 0

    async def test_open_generic_error_translates_fallback_hint(self):
        """T5：非认证/非超时异常（如网络拒连）走兜底翻译（带连接/认证指引）。"""
        device = FakeBroadlinkDevice()
        device.auth_exc = OSError("connection refused")
        ctrl = make_controller(device)
        with pytest.raises(HalError) as ei:
            await ctrl.open()
        assert "连接/认证失败" in str(ei.value)

    async def test_auth_rejected_translates_with_unlock_hint(self):
        device = FakeBroadlinkDevice()
        device.auth_exc = type("AuthenticationError", (Exception,), {})(
            -1, "Authentication failed")
        ctrl = make_controller(device)
        with pytest.raises(HalError) as ei:
            await ctrl.open()
        assert "安全锁" in str(ei.value)

    async def test_discovery_timeout_translates_offline_hint(self):
        async def factory(host, timeout):
            raise type("NetworkTimeoutError", (Exception,), {})(-4000, "timeout")

        ctrl = BroadlinkIrController("192.168.101.27", MAC_STR, device_factory=factory)
        with pytest.raises(HalError) as ei:
            await ctrl.open()
        assert "不在线" in str(ei.value)

    async def test_close_idempotent_and_send_after_close_rejected(self):
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.close()  # 未 open 的 close 幂等
        await ctrl.open()
        await ctrl.close()
        await ctrl.close()
        assert device.close_calls == 1
        with pytest.raises(HalError):
            await ctrl.send(base64.b64encode(b"x").decode())

    async def test_close_tolerates_device_without_aclose(self):
        """F6：设备无 aclose 属性 → close 静默完成并清空 _device。"""
        device = _LegacyDeviceWithoutClose()
        ctrl = make_controller(device)
        await ctrl.open()
        await ctrl.close()  # 不抛
        assert ctrl._device is None

    async def test_close_swallows_aclose_error_and_clears_device(self):
        """F6：aclose 抛错 → close 完成不外抛（不阻断停机栈展开），_device 清空。"""
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()

        async def broken_aclose():
            raise OSError("transport already closed")

        device.aclose = broken_aclose
        await ctrl.close()  # 不抛
        assert ctrl._device is None


# ---------- send：码值把关 ----------


class TestSend:
    async def test_send_invalid_base64_rejected(self):
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        with pytest.raises(HalError):
            await ctrl.send("!!not-base64!!")
        assert device.sent == []

    async def test_send_empty_payload_rejected(self):
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        with pytest.raises(HalError):
            await ctrl.send("")  # 解码为空载荷，拒发
        assert device.sent == []

    async def test_send_before_open_rejected(self):
        ctrl = make_controller(FakeBroadlinkDevice())
        with pytest.raises(HalError) as ei:
            await ctrl.send(base64.b64encode(b"x").decode())
        assert "未连接" in str(ei.value)

    async def test_send_transport_failure_raises_hal_error(self):
        """F4：库异常不得裸上抛（HAL 约定统一 HalError），且不产生半发送记录。"""
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()

        async def broken_send(data):
            raise OSError("network unreachable")

        device.send_data = broken_send
        with pytest.raises(HalError) as ei:
            await ctrl.send(base64.b64encode(b"\x26\x01").decode())
        assert "发送失败" in str(ei.value)
        assert device.sent == []

    async def test_enter_learning_failure_raises_hal_error(self):
        """F4：enter_learning 库异常统一转 HalError（进入学习模式失败指引）。"""
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()

        async def broken_enter():
            raise OSError("no route to host")

        device.enter_learning = broken_enter
        with pytest.raises(HalError) as ei:
            await ctrl.learn(timeout_seconds=0.5)
        assert "进入学习模式失败" in str(ei.value)


# ---------- learn：轮询取码与超时 ----------


class TestLearn:
    async def test_learn_polls_until_code_and_returns_base64(self):
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        device.check_results = [b"", b"", b"\x26\x01\x00\x9d"]
        code = await ctrl.learn(timeout_seconds=5.0)
        assert device.enter_calls == 1
        assert code == base64.b64encode(b"\x26\x01\x00\x9d").decode("ascii")

    async def test_learn_treats_storage_structure_read_errors_as_no_data(self):
        """F5：RM4 固件怪癖——未录到码时回错误码而非空载荷：-5 StorageError /
        -6 StructureAbnormalError / -10 ReadError（python-broadlink 1.0.6 分类）
        均续等不算失败。"""
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        device.check_results = [b"", b"", b"", b"\x26\x01"]
        calls = {"n": 0}
        orig_check = device.check_data

        async def flaky_check():
            calls["n"] += 1
            if calls["n"] == 1:
                raise type("StorageError", (Exception,), {})(-5, "storage is full")
            if calls["n"] == 2:
                raise type("StructureAbnormalError", (Exception,), {})(-6, "empty code")
            if calls["n"] == 3:
                raise type("ReadError", (Exception,), {})(-10, "read failed")
            return await orig_check()

        device.check_data = flaky_check
        code = await ctrl.learn(timeout_seconds=5.0)
        assert code == base64.b64encode(b"\x26\x01").decode("ascii")

    async def test_learn_errno_fallback_tolerates_renamed_error_classes(self):
        """F5 兜底：类名不含 Storage/Read/Structure 但 errno 命中 -5/-6/-10
        同样续等（上游异常改名防漂移）。"""
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        device.check_results = [b"", b"\x26\x01"]
        calls = {"n": 0}
        orig_check = device.check_data

        async def flaky_check():
            calls["n"] += 1
            if calls["n"] == 1:
                exc = type("FutureRenamedError", (Exception,), {})(-6, "renamed upstream")
                exc.errno = -6
                raise exc
            return await orig_check()

        device.check_data = flaky_check
        code = await ctrl.learn(timeout_seconds=5.0)
        assert code == base64.b64encode(b"\x26\x01").decode("ascii")

    async def test_learn_non_storage_error_propagates(self):
        """check_data 的非 Storage 异常不吞——原样上抛（包转话术由上层负责）。"""
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()

        async def broken_check():
            raise type("NetworkTimeoutError", (Exception,), {})(-1, "timeout")

        device.check_data = broken_check
        with pytest.raises(Exception) as ei:
            await ctrl.learn(timeout_seconds=0.5)
        assert type(ei.value).__name__ == "NetworkTimeoutError"

    async def test_learn_timeout_raises_hal_error(self):
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        device.check_results = [b""]
        with pytest.raises(HalError) as ei:
            await ctrl.learn(timeout_seconds=0.05)
        assert "超时" in str(ei.value)

    async def test_learn_before_open_rejected(self):
        ctrl = make_controller(FakeBroadlinkDevice())
        with pytest.raises(HalError):
            await ctrl.learn(timeout_seconds=0.05)


# ---------- parse_mac / config 校验 ----------


class TestParseMac:
    def test_accepts_colon_dash_and_bare_hex(self):
        assert parse_mac("ec:0b:ae:0c:74:e4") == MAC_BYTES
        assert parse_mac("ec-0b-ae-0c-74-e4") == MAC_BYTES
        assert parse_mac("EC0BAE0C74E4") == MAC_BYTES

    def test_rejects_bad_length_and_nonhex(self):
        with pytest.raises(HalError):
            parse_mac("ec:0b:ae")
        with pytest.raises(HalError):
            parse_mac("zz:0b:ae:0c:74:e4")


class TestConfigIr:
    def _load(self, tmp_path, hal_extra: str, env=None):
        yaml_text = (
            "llm:\n  api_key: sk-x\nhal:\n  backend: board\n" + hal_extra
        )
        path = tmp_path / "config.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        return load_config(path, env=env or {})

    def test_ir_defaults_empty(self, tmp_path):
        cfg = self._load(tmp_path, "")
        assert cfg.hal.ir_host == "" and cfg.hal.ir_mac == ""

    def test_ir_pair_configured(self, tmp_path):
        cfg = self._load(tmp_path, "  ir_host: 192.168.101.27\n  ir_mac: ec:0b:ae:0c:74:e4\n")
        assert cfg.hal.ir_host == "192.168.101.27"
        assert cfg.hal.ir_mac == "ec:0b:ae:0c:74:e4"

    def test_host_without_mac_rejected(self, tmp_path):
        with pytest.raises(ConfigError) as ei:
            self._load(tmp_path, "  ir_host: 192.168.101.27\n")
        assert "ir_mac" in str(ei.value)

    def test_mac_without_host_rejected(self, tmp_path):
        with pytest.raises(ConfigError) as ei:
            self._load(tmp_path, "  ir_mac: ec:0b:ae:0c:74:e4\n")
        assert "ir_host" in str(ei.value)

    def test_bad_mac_rejected(self, tmp_path):
        with pytest.raises(ConfigError):
            self._load(tmp_path, "  ir_host: 192.168.101.27\n  ir_mac: hello-world!!\n")

    def test_env_override(self, tmp_path):
        cfg = self._load(tmp_path, "", env={
            "LIRA_HAL_IR_HOST": "192.168.101.27",
            "LIRA_HAL_IR_MAC": "ec:0b:ae:0c:74:e4",
        })
        assert cfg.hal.ir_host == "192.168.101.27"
        assert cfg.hal.ir_mac == "ec:0b:ae:0c:74:e4"

    def test_env_host_without_mac_rejected(self, tmp_path):
        """T7：环境变量只配 ir_host（无 MAC）→ 成对校验同样拦截。"""
        with pytest.raises(ConfigError) as ei:
            self._load(tmp_path, "", env={"LIRA_HAL_IR_HOST": "192.168.101.27"})
        assert "ir_mac" in str(ei.value)


# ---------- 发现探测 CLI（T4：SETUP 2.6 配置信息采集用） ----------


class _FakeDiscovered:
    """discover() 结果替身：host/mac/devtype + auth 可脚本化。"""

    def __init__(self, *, auth_ok: bool = True) -> None:
        self.host = ("192.168.101.27", 80)
        self.mac = MAC_BYTES
        self.devtype = 0x653A
        self.model = "RM4 mini"
        self.name = "客厅"
        self.is_locked = False
        self._auth_ok = auth_ok
        self.auth_calls = 0

    async def auth(self) -> bool:
        self.auth_calls += 1
        if not self._auth_ok:
            raise type("AuthenticationError", (Exception,), {})(-1, "auth failed")
        return True


class _FakeBroadlinkModule:
    """broadlink 模块替身（sys.modules 注入；x86 不装真依赖）。"""

    def __init__(self, devices: list) -> None:
        self._devices = devices

    async def discover(self, timeout: float = 5.0):
        return list(self._devices)


class TestDiscoverCli:
    """`python -m lira.hal.board.ir_broadlink` 退出码约定：0 全认证成功 /
    1 无设备 / 2 有认证失败 / 3 依赖未装。"""

    MODULE = "lira.hal.board.ir_broadlink"

    async def test_no_devices_exits_1(self, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "broadlink", _FakeBroadlinkModule([]))
        rc = await self._run()
        assert rc == 1
        assert "未发现" in capsys.readouterr().out

    async def test_auth_ok_exits_0(self, monkeypatch, capsys):
        device = _FakeDiscovered()
        monkeypatch.setitem(sys.modules, "broadlink", _FakeBroadlinkModule([device]))
        rc = await self._run()
        assert rc == 0
        assert device.auth_calls == 1
        assert "auth OK" in capsys.readouterr().out

    async def test_auth_failed_exits_2(self, monkeypatch, capsys):
        devices = [_FakeDiscovered(auth_ok=True), _FakeDiscovered(auth_ok=False)]
        monkeypatch.setitem(sys.modules, "broadlink", _FakeBroadlinkModule(devices))
        rc = await self._run()
        assert rc == 2
        assert "auth FAILED" in capsys.readouterr().out

    async def test_missing_dependency_exits_3(self, monkeypatch, capsys):
        # sys.modules 槽位置 None → import 抛 ImportError（依赖未装路径）
        monkeypatch.setitem(sys.modules, "broadlink", None)
        rc = await self._run()
        assert rc == 3
        assert "未安装" in capsys.readouterr().err

    async def _run(self) -> int:
        import importlib

        module = importlib.import_module(self.MODULE)
        return await module._discover_cli()
