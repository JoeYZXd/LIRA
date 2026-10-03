"""LIRA 设备端配置加载。

分层规则（计划 U1 Approach）：
  1. 代码内置默认值（`DEFAULTS`）
  2. `config.yaml` 覆盖默认值（路径：`LIRA_CONFIG` 环境变量 > 仓库根 `device/config.yaml`）
  3. 环境变量覆盖 yaml（`LIRA_LLM_BASE_URL` 等，空字符串视为未设置）

设计约束：
  - 隐私模式、高危设备表等**运行时可变状态不入配置文件**——它们属于运行期内存/本地库
    （后续单元实现），因此本模块不含这些字段。
  - 缺失必填项（如 LLM api_key）时抛出带修复指引的 `ConfigError`，绝不静默使用占位值。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

# device/lira/config.py -> device/lira -> device -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "device" / "config.yaml"

# GLM OpenAI 兼容端点（Key Decisions：base_url 覆盖实现 OpenAI 兼容可配置端点）
DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_LLM_MODEL = "glm-4-flash"

HAL_BACKENDS = ("mock", "board")


class ConfigError(Exception):
    """配置缺失/非法。message 必须包含修复指引。"""


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    # U5 熔断降级参数（计划 Deferred: 失败阈值 3 次 / 冷却 60s 起步，运行期调整）
    breaker_failure_threshold: int
    breaker_cooldown_seconds: float
    # R28：远程处理超过该秒数未出首 token 时播报等待反馈
    wait_feedback_seconds: float
    # U5：复杂文本转白话的长度阈值（字符数）；医疗类别词不受阈值限制
    colloquial_threshold_chars: int


@dataclass(frozen=True)
class HalConfig:
    """HAL 实现选择。板上是真实硬件（U10/M7 接线后）；音频/摄像头设备号固化入配置。"""

    backend: str
    mock_images_dir: Path
    mock_audio_dir: Path
    #: 板上采集设备（M4：FY-SP003U）。空串 = 系统默认；数值串 = sounddevice 序号
    mic_device: str = ""
    #: 板上播放设备（M6：板载 ES8388 线路输出）。空串 = PipeWire 默认 sink
    speaker_device: str = ""
    #: 板上摄像头设备（M3：OV13855@CAM1 → /dev/video0）
    camera_device: str = ""


@dataclass(frozen=True)
class UiConfig:
    """设备端 Web UI 监听地址（U7；局域网浏览器访问，无触摸屏）。"""

    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True)
class DevConsoleConfig:
    """开发者控制台开关（默认关，启动时判定；产线不暴露调试面）。"""

    enabled: bool = False


@dataclass(frozen=True)
class SyncConfig:
    """设备↔后台同步（U6/R30 设备侧）。

    ws_url 为空 = 同步未配置（离线纯本地）。device_token 为开发期预置的
    设备鉴权 token（2026-09-27 决议：无配对流程）；ws_url 非空时必填。
    """

    ws_url: str
    heartbeat_seconds: float
    device_token: str


@dataclass(frozen=True)
class AppConfig:
    llm: LlmConfig
    hal: HalConfig
    sync: SyncConfig
    ui: UiConfig
    dev_console: DevConsoleConfig
    db_path: Path
    log_level: str


# 内置默认值 = yaml 缺失时的兜底。api_key 故意不设默认值（必填项）。
DEFAULTS: dict[str, Any] = {
    "llm": {
        "base_url": DEFAULT_GLM_BASE_URL,
        "api_key": "",
        "model": DEFAULT_LLM_MODEL,
        "timeout_seconds": 10.0,
        "breaker_failure_threshold": 3,
        "breaker_cooldown_seconds": 60.0,
        "wait_feedback_seconds": 2.0,
        "colloquial_threshold_chars": 150,
    },
    "hal": {
        "backend": "mock",
        "mock_images_dir": "assets/mock_images",
        "mock_audio_dir": "assets/mock_audio",
        # M7：板上音频/摄像头设备号固化（M3/M4/M6 实测；空串 = 系统默认设备）
        "mic_device": "",
        "speaker_device": "",
        "camera_device": "",
    },
    # U7：设备 Web UI 监听地址（局域网浏览器访问；M7 起 in-process 挂载主循环）
    "ui": {
        "host": "0.0.0.0",
        "port": 8080,
    },
    # M8 计划：开发者控制台（默认关；口令 vault 鉴权 + 采集类测试隐私联锁）
    "dev_console": {
        "enabled": False,
    },
    # U6：本地家电配置库（R13，安全规则本地持久化；gitignore *.db）
    "db_path": "data/device.db",
    # U6：设备↔后台同步（R30 设备侧）。ws_url 空 = 未配置后台（离线纯本地运行）
    "sync": {
        "ws_url": "",
        "heartbeat_seconds": 60.0,
        # 2026-09-27 决议：无配对流程，device token 开发期预置（写入 yaml 或环境变量）
        "device_token": "",
    },
    "log_level": "INFO",
}

# 环境变量覆盖表：env 名 -> (yaml 段, 键, 类型)
ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "LIRA_LLM_BASE_URL": ("llm", "base_url", str),
    "LIRA_LLM_API_KEY": ("llm", "api_key", str),
    "LIRA_LLM_MODEL": ("llm", "model", str),
    "LIRA_LLM_TIMEOUT_SECONDS": ("llm", "timeout_seconds", float),
    "LIRA_LLM_BREAKER_FAILURE_THRESHOLD": ("llm", "breaker_failure_threshold", int),
    "LIRA_LLM_BREAKER_COOLDOWN_SECONDS": ("llm", "breaker_cooldown_seconds", float),
    "LIRA_LLM_WAIT_FEEDBACK_SECONDS": ("llm", "wait_feedback_seconds", float),
    "LIRA_LLM_COLLOQUIAL_THRESHOLD_CHARS": ("llm", "colloquial_threshold_chars", int),
    "LIRA_HAL_BACKEND": ("hal", "backend", str),
    "LIRA_HAL_MIC_DEVICE": ("hal", "mic_device", str),
    "LIRA_HAL_SPEAKER_DEVICE": ("hal", "speaker_device", str),
    "LIRA_HAL_CAMERA_DEVICE": ("hal", "camera_device", str),
    "LIRA_DB_PATH": (None, "db_path", str),
    "LIRA_SYNC_WS_URL": ("sync", "ws_url", str),
    "LIRA_SYNC_HEARTBEAT_SECONDS": ("sync", "heartbeat_seconds", float),
    "LIRA_SYNC_DEVICE_TOKEN": ("sync", "device_token", str),
    "LIRA_UI_HOST": ("ui", "host", str),
    "LIRA_UI_PORT": ("ui", "port", int),
    "LIRA_DEV_CONSOLE": ("dev_console", "enabled", "bool"),
    "LIRA_LOG_LEVEL": (None, "log_level", str),
}


def _resolve_path(value: str) -> Path:
    """配置中的相对路径一律相对仓库根解析（与 cwd 无关，任何目录下启动行为一致）。"""
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    # deepcopy：merged 不得与 DEFAULTS 共享嵌套 dict——否则后续 _apply_env 的
    # env 覆盖会把值永久写进模块级 DEFAULTS，跨 load_config 调用泄漏
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key not in base:
            raise ConfigError(
                f"配置文件包含未知键: {key!r}（允许的顶层键: {sorted(base)}）。"
                "请检查 config.yaml 拼写。"
            )
        if isinstance(base[key], dict):
            if not isinstance(value, Mapping):
                raise ConfigError(f"配置键 {key!r} 应为映射（section），实际为 {type(value).__name__}。")
            merged[key] = _deep_merge(base[key], value)
        else:
            merged[key] = value
    return merged


def _apply_env(config: dict[str, Any], env: Mapping[str, str]) -> None:
    for env_name, (section, key, value_type) in ENV_OVERRIDES.items():
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue  # 未设置/空串 = 未覆盖
        if value_type is float:
            try:
                value: Any = float(raw)
            except ValueError as exc:
                raise ConfigError(f"环境变量 {env_name}={raw!r} 不是合法数字。") from exc
        elif value_type is int:
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigError(f"环境变量 {env_name}={raw!r} 不是合法整数。") from exc
        elif value_type == "bool":
            # 专用分支：bool(raw) 通用转换会 fail-open（bool("false")==True）——
            # "1/true/yes/on"（不区分大小写）为开，其余一律为关
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            value = raw
        if section is None:
            config[key] = value
        else:
            config[section][key] = value


def _validate(config: dict[str, Any], *, require_api_key: bool) -> None:
    llm = config["llm"]
    if not llm["base_url"]:
        raise ConfigError("LLM base_url 为空。请设置 config.yaml 的 llm.base_url 或环境变量 LIRA_LLM_BASE_URL。")
    if require_api_key and not llm["api_key"]:
        raise ConfigError(
            "LLM api_key 缺失（必填项）。"
            "请设置环境变量 LIRA_LLM_API_KEY，或写入 device/config.yaml 的 llm.api_key。"
        )
    if not llm["model"]:
        raise ConfigError("LLM model 为空。请设置 llm.model（如 glm-4-flash）。")
    if llm["timeout_seconds"] <= 0:
        raise ConfigError("llm.timeout_seconds 必须为正数。")
    if int(llm["breaker_failure_threshold"]) < 1:
        raise ConfigError("llm.breaker_failure_threshold 必须 >= 1（连续失败次数开断路）。")
    if llm["breaker_cooldown_seconds"] <= 0:
        raise ConfigError("llm.breaker_cooldown_seconds 必须为正数。")
    if llm["wait_feedback_seconds"] <= 0:
        raise ConfigError("llm.wait_feedback_seconds 必须为正数（R28 等待反馈阈值）。")
    if int(llm["colloquial_threshold_chars"]) < 1:
        raise ConfigError("llm.colloquial_threshold_chars 必须 >= 1。")

    if config["hal"]["backend"] not in HAL_BACKENDS:
        raise ConfigError(
            f"hal.backend={config['hal']['backend']!r} 非法，可选值: {HAL_BACKENDS}。"
        )
    if not config["db_path"]:
        raise ConfigError("db_path 为空。请设置本地家电配置库路径（如 data/device.db）。")
    if config["sync"]["heartbeat_seconds"] <= 0:
        raise ConfigError("sync.heartbeat_seconds 必须为正数（R30 心跳兜底周期）。")
    if config["sync"]["ws_url"] and not config["sync"]["device_token"]:
        raise ConfigError(
            "sync.device_token 缺失（配置了 sync.ws_url 即必填）。"
            "请设置环境变量 LIRA_SYNC_DEVICE_TOKEN，或写入 config.yaml 的 sync.device_token"
            "（后台「注册设备」页面一次性展示的 token）。"
        )

    ui = config["ui"]
    if not 0 <= int(ui["port"]) <= 65535:
        # 0 = 临时端口（测试用）；生产配置应为固定端口
        raise ConfigError("ui.port 必须在 0~65535 之间（0 = 临时端口）。")
    if not ui["host"]:
        raise ConfigError("ui.host 为空（局域网监听地址，如 0.0.0.0）。")

    if not isinstance(config["dev_console"]["enabled"], bool):
        raise ConfigError("dev_console.enabled 必须为布尔值。")

    log_level = str(config["log_level"]).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"log_level={config['log_level']!r} 非法。")
    config["log_level"] = log_level


def load_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    require_api_key: bool = True,
) -> AppConfig:
    """加载配置：内置默认值 <- config.yaml <- 环境变量。

    Args:
        path: 配置文件路径；None 时依次尝试 `LIRA_CONFIG` 环境变量、
              `device/config.yaml`。文件不存在时视为"无覆盖，仅默认值+环境变量"。
        env: 环境变量来源（默认 os.environ），测试可注入。
        require_api_key: False 时允许 api_key 为空（用于 `--dry-run`，
              真正发起远程调用前仍必须配置）。

    Raises:
        ConfigError: yaml 含未知键、类型错误，或必填项缺失。
    """
    env = os.environ if env is None else env

    config_path = Path(path) if path is not None else Path(env.get("LIRA_CONFIG", DEFAULT_CONFIG_PATH))
    file_data: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
        if loaded is not None:
            if not isinstance(loaded, Mapping):
                raise ConfigError(f"{config_path} 顶层必须是 YAML 映射。")
            file_data = dict(loaded)

    config = _deep_merge(DEFAULTS, file_data)
    _apply_env(config, env)
    _validate(config, require_api_key=require_api_key)

    hal = config["hal"]
    return AppConfig(
        llm=LlmConfig(
            base_url=str(config["llm"]["base_url"]),
            api_key=str(config["llm"]["api_key"]),
            model=str(config["llm"]["model"]),
            timeout_seconds=float(config["llm"]["timeout_seconds"]),
            breaker_failure_threshold=int(config["llm"]["breaker_failure_threshold"]),
            breaker_cooldown_seconds=float(config["llm"]["breaker_cooldown_seconds"]),
            wait_feedback_seconds=float(config["llm"]["wait_feedback_seconds"]),
            colloquial_threshold_chars=int(config["llm"]["colloquial_threshold_chars"]),
        ),
        hal=HalConfig(
            backend=str(hal["backend"]),
            mock_images_dir=_resolve_path(str(hal["mock_images_dir"])),
            mock_audio_dir=_resolve_path(str(hal["mock_audio_dir"])),
            mic_device=str(hal["mic_device"]),
            speaker_device=str(hal["speaker_device"]),
            camera_device=str(hal["camera_device"]),
        ),
        sync=SyncConfig(
            ws_url=str(config["sync"]["ws_url"]),
            heartbeat_seconds=float(config["sync"]["heartbeat_seconds"]),
            device_token=str(config["sync"]["device_token"]),
        ),
        ui=UiConfig(
            host=str(config["ui"]["host"]),
            port=int(config["ui"]["port"]),
        ),
        dev_console=DevConsoleConfig(
            enabled=bool(config["dev_console"]["enabled"]),
        ),
        db_path=_resolve_path(str(config["db_path"])),
        log_level=str(config["log_level"]),
    )
