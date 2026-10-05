"""M5 板上红外传输层测试（lira/hal/board/ir_broadlink.py，fake 设备注入）。

覆盖：open 认证与 MAC 核对（happy path + 锁定/超时错误翻译）、send base64 解码
（非法码值/未连接拒发）、learn 轮询取码与超时、close 幂等、config ir_host/ir_mac
成对与格式校验。全部经 device_factory 注入 fake，不依赖 python-broadlink。
"""

from __future__ import annotations

import base64

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

    async def factory(host: str, mac: str, timeout: float):
        device.seen_host = host
        device.seen_timeout = timeout
        return device

    return factory


def make_controller(device: FakeBroadlinkDevice, **kwargs) -> BroadlinkIrController:
    kwargs.setdefault("learn_poll_interval", 0.001)
    kwargs.setdefault("hello_timeout", 0.5)
    return BroadlinkIrController("192.168.101.27", MAC_STR,
                                 device_factory=make_factory(device), **kwargs)


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

    async def test_open_mac_mismatch_rejected(self):
        device = FakeBroadlinkDevice(mac=bytes.fromhex("aabbccddeeff"))
        ctrl = make_controller(device)
        with pytest.raises(HalError) as ei:
            await ctrl.open()
        assert "MAC 不符" in str(ei.value)

    async def test_auth_rejected_translates_with_unlock_hint(self):
        device = FakeBroadlinkDevice()
        device.auth_exc = type("AuthenticationError", (Exception,), {})(
            -1, "Authentication failed")
        ctrl = make_controller(device)
        with pytest.raises(HalError) as ei:
            await ctrl.open()
        assert "安全锁" in str(ei.value)

    async def test_discovery_timeout_translates_offline_hint(self):
        async def factory(host, mac, timeout):
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

    async def test_learn_treats_storage_and_read_errors_as_no_data(self):
        """RM4 固件怪癖：未录到码时回 -5 StorageError/-6 ReadError 而非空载荷——续等不算失败。"""
        device = FakeBroadlinkDevice()
        ctrl = make_controller(device)
        await ctrl.open()
        device.check_results = [b"", b"", b"\x26\x01"]
        calls = {"n": 0}
        orig_check = device.check_data

        async def flaky_check():
            calls["n"] += 1
            if calls["n"] == 1:
                raise type("StorageError", (Exception,), {})(-5, "storage is full")
            if calls["n"] == 2:
                raise type("ReadError", (Exception,), {})(-6, "empty code")
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
