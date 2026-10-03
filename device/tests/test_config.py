"""U1 配置加载测试（Test scenarios: yaml 默认值 / 环境变量覆盖 / 缺失必填项报错）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lira.config import (
    DEFAULT_GLM_BASE_URL,
    REPO_ROOT,
    AppConfig,
    ConfigError,
    load_config,
)

FULL_YAML = """\
llm:
  base_url: https://example.invalid/v1
  api_key: sk-test-123
  model: glm-4-flash
  timeout_seconds: 5.0
hal:
  backend: mock
  mock_images_dir: assets/mock_images
  mock_audio_dir: assets/mock_audio
log_level: DEBUG
"""


def write_cfg(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestHappyPath:
    def test_full_yaml_fields_present(self, tmp_path):
        cfg = load_config(write_cfg(tmp_path, FULL_YAML), env={})
        assert isinstance(cfg, AppConfig)
        assert cfg.llm.base_url == "https://example.invalid/v1"
        assert cfg.llm.api_key == "sk-test-123"
        assert cfg.llm.model == "glm-4-flash"
        assert cfg.llm.timeout_seconds == 5.0
        assert cfg.hal.backend == "mock"
        assert cfg.log_level == "DEBUG"

    def test_default_glm_base_url_when_yaml_omits_it(self, tmp_path):
        yaml_text = "llm:\n  api_key: sk-x\nhal:\n  backend: mock\n"
        cfg = load_config(write_cfg(tmp_path, yaml_text), env={})
        assert cfg.llm.base_url == DEFAULT_GLM_BASE_URL
        assert cfg.llm.model == "glm-4-flash"  # 内置默认值兜底

    def test_repo_default_config_loads(self):
        """仓库自带 device/config.yaml 可加载（api_key 留空须豁免）。"""
        cfg = load_config(env={}, require_api_key=False)
        assert cfg.llm.base_url == DEFAULT_GLM_BASE_URL
        assert cfg.hal.backend == "mock"

    def test_relative_paths_resolved_against_repo_root(self, tmp_path):
        cfg = load_config(write_cfg(tmp_path, FULL_YAML), env={})
        assert cfg.hal.mock_images_dir == REPO_ROOT / "assets" / "mock_images"
        assert cfg.hal.mock_audio_dir == REPO_ROOT / "assets" / "mock_audio"

    def test_board_devices_and_ui_defaults(self, tmp_path):
        """M7 新增字段：板上设备号默认空串（系统默认设备），UI 默认 0.0.0.0:8080。"""
        cfg = load_config(write_cfg(tmp_path, FULL_YAML), env={})
        assert cfg.hal.mic_device == ""
        assert cfg.hal.speaker_device == ""
        assert cfg.hal.camera_device == ""
        assert cfg.ui.host == "0.0.0.0"
        assert cfg.ui.port == 8080

    def test_board_devices_and_ui_from_yaml(self, tmp_path):
        yaml_text = (
            "llm:\n  api_key: k\n"
            "hal:\n  backend: board\n"
            "  mic_device: '3'\n  speaker_device: '2'\n  camera_device: /dev/video1\n"
            "ui:\n  host: 192.168.1.5\n  port: 9090\n"
        )
        cfg = load_config(write_cfg(tmp_path, yaml_text), env={})
        assert cfg.hal.backend == "board"
        assert cfg.hal.mic_device == "3"
        assert cfg.hal.speaker_device == "2"
        assert cfg.hal.camera_device == "/dev/video1"
        assert cfg.ui.host == "192.168.1.5"
        assert cfg.ui.port == 9090


class TestEnvOverride:
    def test_env_overrides_yaml_values(self, tmp_path):
        env = {
            "LIRA_LLM_BASE_URL": "https://other.endpoint/api",
            "LIRA_LLM_API_KEY": "sk-from-env",
            "LIRA_LLM_MODEL": "glm-4-plus",
            "LIRA_LLM_TIMEOUT_SECONDS": "30",
            "LIRA_HAL_BACKEND": "mock",
            "LIRA_LOG_LEVEL": "warning",
        }
        cfg = load_config(write_cfg(tmp_path, FULL_YAML), env=env)
        assert cfg.llm.base_url == "https://other.endpoint/api"
        assert cfg.llm.api_key == "sk-from-env"
        assert cfg.llm.model == "glm-4-plus"
        assert cfg.llm.timeout_seconds == 30.0
        assert cfg.log_level == "WARNING"  # 归一化为大写

    def test_env_provides_required_api_key_when_yaml_empty(self, tmp_path):
        yaml_text = "llm:\n  api_key: ''\n"
        cfg = load_config(write_cfg(tmp_path, yaml_text), env={"LIRA_LLM_API_KEY": "sk-env"})
        assert cfg.llm.api_key == "sk-env"

    def test_empty_env_string_does_not_override(self, tmp_path):
        """空字符串环境变量 = 未设置，不覆盖 yaml。"""
        env = {"LIRA_LLM_BASE_URL": "", "LIRA_LLM_MODEL": ""}
        cfg = load_config(write_cfg(tmp_path, FULL_YAML), env=env)
        assert cfg.llm.base_url == "https://example.invalid/v1"
        assert cfg.llm.model == "glm-4-flash"

    def test_lira_config_env_selects_file(self, tmp_path, monkeypatch):
        path = write_cfg(tmp_path, FULL_YAML)
        cfg = load_config(env={"LIRA_CONFIG": str(path)})
        assert cfg.llm.api_key == "sk-test-123"

    def test_invalid_timeout_number_reports_clearly(self, tmp_path):
        with pytest.raises(ConfigError, match="LIRA_LLM_TIMEOUT_SECONDS"):
            load_config(write_cfg(tmp_path, FULL_YAML), env={"LIRA_LLM_TIMEOUT_SECONDS": "abc"})

    def test_env_overrides_board_devices_and_ui(self, tmp_path):
        env = {
            "LIRA_HAL_MIC_DEVICE": "FY-SP003U",
            "LIRA_HAL_SPEAKER_DEVICE": "2",
            "LIRA_HAL_CAMERA_DEVICE": "/dev/video0",
            "LIRA_UI_PORT": "8123",
        }
        cfg = load_config(write_cfg(tmp_path, FULL_YAML), env=env)
        assert cfg.hal.mic_device == "FY-SP003U"
        assert cfg.hal.speaker_device == "2"
        assert cfg.hal.camera_device == "/dev/video0"
        assert cfg.ui.port == 8123


class TestErrorPaths:
    def test_missing_api_key_raises_with_hint(self, tmp_path):
        yaml_text = "llm:\n  base_url: https://x\n"
        with pytest.raises(ConfigError) as exc_info:
            load_config(write_cfg(tmp_path, yaml_text), env={})
        message = str(exc_info.value)
        assert "api_key" in message
        assert "LIRA_LLM_API_KEY" in message  # 报错包含修复指引

    def test_dry_run_allows_empty_api_key(self, tmp_path):
        yaml_text = "llm:\n  base_url: https://x\n"
        cfg = load_config(write_cfg(tmp_path, yaml_text), env={}, require_api_key=False)
        assert cfg.llm.api_key == ""

    def test_unknown_yaml_key_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="未知键"):
            load_config(write_cfg(tmp_path, FULL_YAML + "\nllm_typo:\n  key: v\n"), env={})

    def test_unknown_nested_yaml_key_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="未知键"):
            load_config(write_cfg(tmp_path, "llm:\n  api_key: k\n  base_urll: x\n"), env={})

    def test_invalid_hal_backend_raises(self, tmp_path):
        yaml_text = "llm:\n  api_key: k\nhal:\n  backend: virtual\n"
        with pytest.raises(ConfigError, match="hal.backend"):
            load_config(write_cfg(tmp_path, yaml_text), env={})

    def test_invalid_ui_port_raises(self, tmp_path):
        yaml_text = "llm:\n  api_key: k\nui:\n  port: 70000\n"
        with pytest.raises(ConfigError, match="ui.port"):
            load_config(write_cfg(tmp_path, yaml_text), env={})

    def test_non_mapping_yaml_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="映射"):
            load_config(write_cfg(tmp_path, "- a\n- b\n"), env={})

    def test_missing_config_file_is_defaults_plus_env(self, tmp_path):
        """文件不存在时不报错：仅内置默认值 + 环境变量。"""
        cfg = load_config(env={"LIRA_LLM_API_KEY": "sk-e"})
        assert cfg.llm.base_url == DEFAULT_GLM_BASE_URL
        assert cfg.llm.api_key == "sk-e"

    def test_env_override_does_not_pollute_defaults(self, tmp_path):
        """回归（M7 接线时发现）：env 覆盖不得写入模块级 DEFAULTS——
        同进程第二次 load_config 不得继承上一次的 env 值。"""
        cfg1 = load_config(env={"LIRA_LLM_API_KEY": "sk-leak"})
        assert cfg1.llm.api_key == "sk-leak"
        from lira.config import DEFAULTS

        assert DEFAULTS["llm"]["api_key"] == "", "DEFAULTS 被 env 覆盖污染"
        cfg2 = load_config(env={}, require_api_key=False)
        assert cfg2.llm.api_key == "", "env 覆盖跨 load_config 调用泄漏"
