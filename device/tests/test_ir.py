"""U6 红外控制服务测试（计划 Test scenarios 前 5 条 + G12 状态机接线）。

覆盖：AE5 学习→命中→回放（mock 断言）、场景逐项报告（R29）、别名双命中澄清
（G12，phrasebook 澄清文案 + 状态机接线）、未配置设备话术、禁用设备防御性拒发
（绕过状态机直调 send_ir）、高危未确认拒发（双层 fail-closed 第二层）。
"""

from __future__ import annotations

import asyncio

import pytest

from lira.appliances.ir import ApplianceError, IRService, LearningManager
from lira.appliances.models import ApplianceModel, SceneModel, SceneStep
from lira.appliances.store import ApplianceStore
from lira.dialog import AudioRoute, DialogEngine, Router, State, phrasebook as pb
from lira.hal.base import HalError
from lira.hal.mock.ir import MockIrController


class SlowMockIr(MockIrController):
    """learn 可阻塞的 mock（学习会话并发测试用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def learn(self, timeout_seconds: float = 10.0) -> str:
        await asyncio.wait_for(self.release.wait(), timeout=timeout_seconds)
        return self.learn_queue.pop(0)


def make_store(tmp_path) -> ApplianceStore:
    store = ApplianceStore(tmp_path / "device.db")
    store.upsert_appliance(ApplianceModel(
        name="台灯", aliases=("台灯",), actions={"打开": ("打开",)},
        codes={"打开": "pulse 9000"}))
    store.upsert_appliance(ApplianceModel(
        name="取暖器", aliases=("取暖器",), actions={"打开": ("打开",)},
        codes={"打开": "pulse 7000"}, is_high_risk=True))
    store.upsert_appliance(ApplianceModel(
        name="加湿器", aliases=("加湿器",), actions={"打开": ("打开",)},
        codes={"打开": "pulse 6000"}, enabled=False))
    store.upsert_appliance(ApplianceModel(
        name="空调", aliases=("空调",), actions={"制冷": ("制冷",), "制热": ("制热",)}))
    store.upsert_scene(SceneModel(name="睡觉模式", steps=(
        SceneStep("台灯", "关闭"), SceneStep("空调", "制冷"), SceneStep("加湿器", "打开"))))
    store.upsert_code("台灯", "关闭", "pulse 8000")
    return store


# ---------- 回放与安全闸（双层 fail-closed 第二层） ----------


class TestSendIrGates:
    async def test_low_risk_sends_code(self, tmp_path):
        store = make_store(tmp_path)
        ir = MockIrController()
        svc = IRService(store, ir)
        await svc.send_ir("台灯", "打开")
        assert ir.sent_codes == ["pulse 9000"]

    async def test_unknown_device_rejected(self, tmp_path):
        svc = IRService(make_store(tmp_path), MockIrController())
        with pytest.raises(ApplianceError) as ei:
            await svc.send_ir("电视", "打开")
        assert ei.value.kind == "device_not_found"

    async def test_unknown_action_rejected(self, tmp_path):
        svc = IRService(make_store(tmp_path), MockIrController())
        with pytest.raises(ApplianceError) as ei:
            await svc.send_ir("台灯", "调暗")
        assert ei.value.kind == "action_not_found"

    async def test_missing_code_rejected(self, tmp_path):
        """设备已配置但该动作尚未学习码值。"""
        svc = IRService(make_store(tmp_path), MockIrController())
        with pytest.raises(ApplianceError) as ei:
            await svc.send_ir("空调", "制冷")
        assert ei.value.kind == "code_missing"

    async def test_disabled_device_rejected_even_on_direct_call(self, tmp_path):
        """防御性测试：绕过状态机直调 send_ir，禁用设备同样拒发、IR 未发射。"""
        store = make_store(tmp_path)
        ir = MockIrController()
        svc = IRService(store, ir)
        with pytest.raises(ApplianceError) as ei:
            await svc.send_ir("加湿器", "打开")
        assert ei.value.kind == "disabled"
        assert ir.sent_codes == []

    async def test_high_risk_unconfirmed_rejected(self, tmp_path):
        """最后一道闸：高危且未确认 → 拒发（状态机层校验之外）。"""
        ir = MockIrController()
        svc = IRService(make_store(tmp_path), ir)
        with pytest.raises(ApplianceError) as ei:
            await svc.send_ir("取暖器", "打开", confirmed=False)
        assert ei.value.kind == "confirm_required"
        assert ir.sent_codes == []

    async def test_high_risk_confirmed_sends(self, tmp_path):
        ir = MockIrController()
        svc = IRService(make_store(tmp_path), ir)
        await svc.send_ir("取暖器", "打开", confirmed=True)
        assert ir.sent_codes == ["pulse 7000"]


# ---------- 学习（AE5 / R32） ----------


class TestLearn:
    async def test_learn_stores_code_then_voice_hits_and_replays(self, tmp_path):
        """Covers AE5: 学习"制冷"码 → 入库 → 命中 → 回放（mock 断言）。"""
        store = make_store(tmp_path)
        ir = MockIrController()
        ir.learn_queue = ["pulse 1234 5678"]
        svc = IRService(store, ir)
        code = await svc.learn("空调", "制冷")
        assert code == "pulse 1234 5678"
        assert store.get_appliance("空调").codes["制冷"] == "pulse 1234 5678"
        # 语音层命中（经 to_dialog 后本地规则可匹配）并回放
        await svc.send_ir("空调", "制冷", confirmed=False)
        # mock 学习也走 sent_codes 日志通道（"<learned>" 前缀），最后一条是真实回放
        assert ir.send_count == 1
        assert ir.sent_codes[-1] == "pulse 1234 5678"

    async def test_relearn_overwrites(self, tmp_path):
        store = make_store(tmp_path)
        ir = MockIrController()
        svc = IRService(store, ir)
        ir.learn_queue = ["code-a"]
        await svc.learn("空调", "制冷")
        ir.learn_queue = ["code-b"]
        await svc.learn("空调", "制冷")  # 重学习覆盖同设备+动作
        assert store.get_appliance("空调").codes["制冷"] == "code-b"

    async def test_learn_unknown_device(self, tmp_path):
        svc = IRService(make_store(tmp_path), MockIrController())
        with pytest.raises(ApplianceError) as ei:
            await svc.learn("电视", "打开")
        assert ei.value.kind == "device_not_found"

    async def test_learn_timeout_translates_hal_error(self, tmp_path):
        store = make_store(tmp_path)
        ir = MockIrController()  # learn_queue 空 → 超时 HalError
        svc = IRService(store, ir, learn_timeout=0.01)
        with pytest.raises(ApplianceError) as ei:
            await svc.learn("空调", "制冷")
        assert ei.value.kind == "learn_failed"
        assert not isinstance(ei.value, HalError)


class TestLearningManager:
    async def test_begin_waits_for_code_then_stores(self, tmp_path):
        store = make_store(tmp_path)
        ir = SlowMockIr()
        ir.learn_queue = ["late-code"]
        mgr = LearningManager(IRService(store, ir, learn_timeout=5.0))
        task = mgr.begin("空调", "制热")
        assert mgr.active is True
        await asyncio.sleep(0.01)
        assert mgr.active is True  # 仍在等码
        ir.release.set()
        assert await task == "late-code"
        assert store.get_appliance("空调").codes["制热"] == "late-code"

    async def test_second_begin_rejected_while_active(self, tmp_path):
        ir = SlowMockIr()
        mgr = LearningManager(IRService(make_store(tmp_path), ir, learn_timeout=5.0))
        mgr.begin("空调", "制冷")
        with pytest.raises(ApplianceError) as ei:
            mgr.begin("台灯", "打开")
        assert ei.value.kind == "learning_busy"
        ir.release.set()
        await asyncio.sleep(0.01)


# ---------- 场景逐项回放（R29） ----------


class TestSceneRun:
    async def test_scene_reports_per_step(self, tmp_path):
        """睡觉模式 3 设备：逐项回放、逐项报告。空调未学码 → 如实报告不中断。"""
        store = make_store(tmp_path)
        ir = MockIrController()
        svc = IRService(store, ir)
        results = await svc.run_scene("睡觉模式")
        assert [(r.device, r.ok, r.error_kind) for r in results] == [
            ("台灯", True, None),          # 台灯"关闭"已有码（make_store 末尾补）
            ("空调", False, "code_missing"),
            ("加湿器", False, "disabled"),  # 禁用设备在场景里同样拒发
        ]
        assert ir.sent_codes == ["pulse 8000"]

    async def test_scene_with_codes_sends_all(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_code("空调", "制冷", "frame-cool")
        store.upsert_code("加湿器", "打开", "frame-hum")
        ir = MockIrController()
        svc = IRService(store, ir)
        results = await svc.run_scene("睡觉模式", confirmed=True)
        # 台灯"关闭"已补码（make_store 内）；加湿器禁用仍拒发（场景不豁免安全闸）
        assert [(r.device, r.ok) for r in results] == [("台灯", True), ("空调", True), ("加湿器", False)]
        assert ir.sent_codes == ["pulse 8000", "frame-cool"]

    async def test_scene_high_risk_requires_confirmed(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_appliance(ApplianceModel(
            name="取暖器", aliases=("取暖器",), actions={"打开": ("打开",)},
            codes={"打开": "pulse 7000"}, is_high_risk=True))
        store.upsert_scene(SceneModel(name="取暖", steps=(SceneStep("取暖器", "打开"),)))
        ir = MockIrController()
        svc = IRService(store, ir)
        (r,) = await svc.run_scene("取暖", confirmed=False)
        assert r.ok is False and r.error_kind == "confirm_required"
        assert ir.sent_codes == []
        (r,) = await svc.run_scene("取暖", confirmed=True)
        assert r.ok is True and ir.sent_codes == ["pulse 7000"]

    async def test_unknown_scene(self, tmp_path):
        svc = IRService(make_store(tmp_path), MockIrController())
        with pytest.raises(ApplianceError) as ei:
            await svc.run_scene("观影模式")
        assert ei.value.kind == "scene_not_found"


# ---------- G12 状态机接线（澄清/未配置话术） ----------


class Recorder:
    """纯内存回调记录器（与 test_state_machine 同款，仅保留所需子集）。"""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.ir: list[tuple[str, str]] = []
        self.routes: list[AudioRoute] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def start_listening(self) -> None:
        pass

    def set_audio_route(self, route: AudioRoute) -> None:
        self.routes.append(route)

    def start_capture(self) -> None:
        pass

    def send_ir(self, device: str, action: str) -> None:
        self.ir.append((device, action))

    def playback_pause(self) -> None: ...

    def playback_resume(self) -> None: ...

    def playback_stop(self) -> None: ...

    def playback_read_again(self) -> None: ...

    def playback_volume_up(self) -> None: ...

    def playback_volume_down(self) -> None: ...


def make_engine(appliances) -> tuple[DialogEngine, Recorder]:
    rec = Recorder()
    engine = DialogEngine(router=Router(appliances=appliances), callbacks=rec)
    return engine, rec


class TestG12ClarificationWiring:
    async def test_ambiguous_alias_asks_clarification_and_does_not_execute(self):
        """Covers G12: 两个设备别名同时命中 → 不执行，话术请求澄清，保持聆听。"""
        from lira.dialog.intents import Appliance

        appliances = (
            Appliance(name="台灯", aliases=("灯",), actions={"打开": ("打开",)}),
            Appliance(name="吊灯", aliases=("灯",), actions={"打开": ("打开",)}),
        )
        engine, rec = make_engine(appliances)
        await engine.on_wake()
        await engine.on_asr_text("打开灯")
        assert rec.ir == []  # 不猜、不执行
        assert rec.spoken[-1] == pb.APPLIANCE_AMBIGUOUS
        assert engine.state is State.LISTENING  # 留在聆听态，可换说法
        engine.tick(engine.now + engine.LISTEN_WINDOW_SECONDS + 1)
        assert engine.state is State.LISTENING  # 澄清窗口重新计时（未超时）

    async def test_unique_alias_still_executes_normally(self):
        from lira.dialog.intents import Appliance

        appliances = (Appliance(name="台灯", aliases=("台灯",), actions={"打开": ("打开",)}),)
        engine, rec = make_engine(appliances)
        await engine.on_wake()
        await engine.on_asr_text("打开台灯")
        assert rec.ir == [("台灯", "打开")]
        assert pb.APPLIANCE_AMBIGUOUS not in rec.spoken

    async def test_unconfigured_device_gets_backend_hint(self):
        """指令指向未配置设备（"打开电视"）→ "请家人在后台添加"话术。"""
        store_appliances = tuple(
            a.to_dialog()
            for a in [
                ApplianceModel(name="台灯", aliases=("台灯",), actions={"打开": ("打开",)}),
            ]
        )
        engine, rec = make_engine(store_appliances)
        await engine.on_wake()
        await engine.on_asr_text("打开电视")
        assert rec.ir == []
        assert rec.spoken[-1] == pb.APPLIANCE_NOT_CONFIGURED
        assert engine.state is State.STANDBY

    async def test_no_action_word_still_falls_back_to_r24_guidance(self):
        """不含动作触发词的无关语音 → 仍走 R24 识别失败引导（不误报未配置）。"""
        from lira.dialog.intents import Appliance

        appliances = (Appliance(name="台灯", aliases=("台灯",), actions={"打开": ("打开",)}),)
        engine, rec = make_engine(appliances)
        await engine.on_wake()
        await engine.on_asr_text("今天天气怎么样")
        assert pb.APPLIANCE_NOT_CONFIGURED not in rec.spoken
        assert engine.state is State.LISTENING  # R24 引导后仍聆听
