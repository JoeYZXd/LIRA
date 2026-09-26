"""U6 家电模型与本地库测试（计划 U6 Approach：UPSERT 覆盖、级联删除、原子快照）。

覆盖：设备/动作/码值模型与 dialog.intents 对齐、SQLite(WAL+FULL) 本地持久化、
码值 UPSERT（重学习覆盖）、删除设备级联清码值与场景引用、快照单事务原子应用
（中途失败整体回滚）、(epoch, version) 元数据读写。
"""

from __future__ import annotations

import pytest

from lira.appliances.models import ApplianceModel, SceneModel, SceneStep
from lira.appliances.store import ApplianceStore
from lira.dialog.intents import IntentKind, match_local


def make_store(tmp_path) -> ApplianceStore:
    return ApplianceStore(tmp_path / "device.db")


def make_light(**overrides) -> ApplianceModel:
    kwargs = dict(
        name="台灯",
        aliases=("台灯",),
        actions={"打开": ("打开", "开一下"), "关闭": ("关闭",)},
        codes={"打开": "pulse 9000", "关闭": "pulse 8000"},
    )
    kwargs.update(overrides)
    return ApplianceModel(**kwargs)


# ---------- 模型与意图层对齐 ----------


class TestModelMapping:
    def test_to_dialog_field_alignment(self):
        model = make_light(is_high_risk=True, enabled=False)
        dialog = model.to_dialog()
        assert dialog.name == "台灯"
        assert dialog.aliases == ("台灯",)
        assert dialog.actions == {"打开": ("打开", "开一下"), "关闭": ("关闭",)}
        assert dialog.is_high_risk is True
        assert dialog.enabled is False
        # 码值不进入意图层
        assert not hasattr(dialog, "codes")

    def test_model_usable_for_local_rule_match(self):
        model = make_light()
        intent = match_local("打开台灯", (model.to_dialog(),))
        assert intent is not None and intent.kind is IntentKind.APPLIANCE
        assert intent.action == "打开"

    def test_dto_roundtrip_excludes_codes(self):
        model = make_light()
        dto = model.to_dto()
        assert not hasattr(dto, "codes") or "codes" not in dto.to_json()
        rebuilt = ApplianceModel.from_dto(dto)
        assert rebuilt.actions == model.actions
        assert rebuilt.codes == {}


# ---------- 持久化 ----------


class TestAppliancePersistence:
    def test_upsert_and_get_roundtrip(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_appliance(make_light())
        got = store.get_appliance("台灯")
        assert got is not None
        assert got.codes == {"打开": "pulse 9000", "关闭": "pulse 8000"}
        assert got.is_high_risk is False and got.enabled is True

    def test_upsert_code_overwrites_relearn(self, tmp_path):
        """码值 UPSERT：重学习覆盖同设备+动作。"""
        store = make_store(tmp_path)
        store.upsert_appliance(make_light())
        store.upsert_code("台灯", "打开", "pulse 1111")
        assert store.get_appliance("台灯").codes["打开"] == "pulse 1111"
        store.upsert_code("台灯", "打开", "pulse 2222")
        assert store.get_appliance("台灯").codes["打开"] == "pulse 2222"

    def test_upsert_code_unknown_device_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(KeyError):
            store.upsert_code("空调", "制冷", "pulse 1")

    def test_upsert_code_can_add_new_action(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_appliance(make_light())
        store.upsert_code("台灯", "调亮", "pulse 5")
        got = store.get_appliance("台灯")
        assert got.codes["调亮"] == "pulse 5"
        # 动作存在但无触发词 → 语音不可触发，等待后台配置触发词
        assert got.actions["调亮"] == ()

    def test_snapshot_update_preserves_codes_and_drops_removed_actions(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_appliance(make_light())
        # 后台快照更新：去掉"关闭"动作、改名触发词；不含码值
        store.upsert_appliance(
            ApplianceModel(name="台灯", aliases=("台灯",), actions={"打开": ("打开",)})
        )
        got = store.get_appliance("台灯")
        assert got.codes["打开"] == "pulse 9000"  # 学习成果不丢
        assert "关闭" not in got.actions and "关闭" not in got.codes

    def test_delete_appliance_cascades_actions_and_scene_refs(self, tmp_path):
        """删除设备级联删码值与场景引用（防悬挂）。"""
        store = make_store(tmp_path)
        store.upsert_appliance(make_light())
        store.upsert_appliance(
            ApplianceModel(name="空调", aliases=("空调",), actions={"制冷": ("制冷",)},
                           codes={"制冷": "frame 1"})
        )
        store.upsert_scene(SceneModel(name="睡觉模式", steps=(SceneStep("台灯", "关闭"), SceneStep("空调", "制冷"))))
        assert store.delete_appliance("台灯") is True
        assert store.get_appliance("台灯") is None
        scene = store.get_scene("睡觉模式")
        assert [ (s.device, s.action) for s in scene.steps ] == [("空调", "制冷")]

    def test_wal_and_full_synchronous(self, tmp_path):
        """R13 纪律：WAL + synchronous=FULL。"""
        store = make_store(tmp_path)
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync_level = store._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert mode == "wal"
        assert sync_level == 2  # FULL


# ---------- 场景 ----------


class TestScenePersistence:
    def test_scene_roundtrip(self, tmp_path):
        store = make_store(tmp_path)
        scene = SceneModel(name="睡觉模式", steps=(SceneStep("台灯", "关闭"), SceneStep("空调", "制冷")))
        store.upsert_scene(scene)
        got = store.get_scene("睡觉模式")
        assert got is not None
        assert [(s.device, s.action) for s in got.steps] == [("台灯", "关闭"), ("空调", "制冷")]

    def test_scene_upsert_replaces_steps(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_scene(SceneModel(name="回家", steps=(SceneStep("台灯", "打开"),)))
        store.upsert_scene(SceneModel(name="回家", steps=(SceneStep("空调", "制冷"), SceneStep("台灯", "打开"))))
        assert len(store.get_scene("回家").steps) == 2

    def test_delete_scene(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_scene(SceneModel(name="回家", steps=(SceneStep("台灯", "打开"),)))
        assert store.delete_scene("回家") is True
        assert store.get_scene("回家") is None
        assert store.delete_scene("回家") is False


# ---------- 快照原子应用与元数据 ----------


class TestSnapshotApplyAndMeta:
    def test_apply_snapshot_replaces_all_and_sets_meta(self, tmp_path):
        store = make_store(tmp_path)
        store.upsert_appliance(make_light())  # 旧数据应被全量替换
        store.apply_snapshot(
            appliances=[make_light(enabled=False, is_high_risk=True),
                        ApplianceModel(name="空调", aliases=("空调",), actions={"制冷": ("制冷",)})],
            scenes=[SceneModel(name="睡觉", steps=(SceneStep("空调", "制冷"),))],
            epoch=7,
            version=3,
        )
        names = {a.name for a in store.get_all_appliances()}
        assert names == {"台灯", "空调"}
        assert store.current_epoch() == 7
        assert store.current_version() == 3

    def test_apply_snapshot_atomic_rollback_on_failure(self, tmp_path):
        """应用中途失败 → 整体回滚，本地保持最近完整版本（System-Wide Impact）。"""
        store = make_store(tmp_path)
        store.apply_snapshot(
            appliances=[make_light()], scenes=[], epoch=1, version=1
        )
        broken: list[ApplianceModel] = [make_light(enabled=False)]
        broken.append(ApplianceModel(name="坏数据", aliases=None))  # 非法模型，中途触发 TypeError
        with pytest.raises(TypeError):
            store.apply_snapshot(appliances=broken, scenes=[], epoch=2, version=1)
        # 回滚断言：库中仍是 epoch=1 的完整快照，且台灯未被改成 enabled=False
        assert store.current_epoch() == 1
        assert store.current_version() == 1
        assert store.get_appliance("台灯").enabled is True

    def test_meta_device_token(self, tmp_path):
        store = make_store(tmp_path)
        assert store.device_token() is None
        store.set_meta("device_token", "tok-1")
        assert store.device_token() == "tok-1"

    def test_reopen_keeps_data(self, tmp_path):
        path = tmp_path / "device.db"
        store = ApplianceStore(path)
        store.upsert_appliance(make_light())
        store.close()
        store2 = ApplianceStore(path)
        assert store2.get_appliance("台灯") is not None
        store2.close()
