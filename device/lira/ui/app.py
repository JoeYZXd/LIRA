"""设备本地触摸屏 Web UI（U7，R17/R12/R19）。

定位（计划 U7 Approach）：设备本地小 Web 服务，板上绑屏即 kiosk。

页面（服务端渲染 Jinja2，无前端构建链，无 HTMX——表单 + 重定向已够用）：
  - `/`        状态卡（网络/远程可用/隐私）+ 隐私开关 + 音量/语速 + 设备列表只读
  - `/passphrase/setup`  设备本地口令首启设置（无默认值，fail-closed）
  - `/status`  只读状态 JSON（kiosk 轮询用，不鉴权）

鉴权纪律（与物理按键通道的区分，R19）：
  - 隐私开关的 UI 路径必须出示设备本地口令（`PassphraseVault.verify`，
    scrypt + 常数时间比较）；口令未设置时一律 303 引导到设置页（fail-closed）。
  - 物理按键路径不经过本模块任何校验——两条路径殊途同归，都只触碰同一个
    `PrivacyState` 对象；后果播报是隐私状态的订阅者（装配层挂接），与入口无关。
  - 状态卡/设备列表只读，不鉴权。

日志纪律：本模块只记路由级事件（来源、结果），不记表单内容（含口令）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lira.appliances.store import ApplianceStore
from lira.privacy import PassphraseVault, PrivacyState

__all__ = ["DeviceSettings", "UiServices", "create_app", "TEMPLATES_DIR"]

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class DeviceSettings:
    """音量 / TTS 语速的运行时可变设置（线程安全；持久化留给后续单元）。

    边界拒绝：越界值抛 ValueError（UI 层转为 400），不静默夹紧——
    家属在屏上设错时应当被明确告知，而不是悄悄改成功。
    """

    VOLUME_RANGE = (0.1, 2.0)
    SPEED_RANGE = (0.5, 2.0)

    def __init__(self, volume: float = 1.0, tts_speed: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._volume = volume
        self._tts_speed = tts_speed

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def tts_speed(self) -> float:
        return self._tts_speed

    @staticmethod
    def _check(value: float, bounds: tuple[float, float], name: str) -> float:
        low, high = bounds
        if not low <= value <= high:
            raise ValueError(f"{name} 须在 {low}~{high} 之间。")
        return value

    def set_volume(self, value: float) -> None:
        with self._lock:
            self._volume = self._check(value, self.VOLUME_RANGE, "音量")

    def set_tts_speed(self, value: float) -> None:
        with self._lock:
            self._tts_speed = self._check(value, self.SPEED_RANGE, "语速")


@dataclass
class UiServices:
    """UI 对设备能力的全部访问面（装配层注入；测试用 mock/内存实现）。"""

    privacy: PrivacyState
    vault: PassphraseVault
    store: ApplianceStore
    settings: DeviceSettings = field(default_factory=DeviceSettings)
    #: R10 单一"远程可用性"信号（联网 + 熔断闭合）
    remote_available: Callable[[], bool] = lambda: False
    #: 基础联网检测（状态卡区分"无网"与"远程服务不可用"）
    network_ok: Callable[[], bool] = lambda: False


def _appliance_rows(services: UiServices) -> list[dict[str, object]]:
    """设备列表只读视图（不暴露码值——原始码不是屏上该出现的内容）。"""
    rows: list[dict[str, object]] = []
    for a in services.store.get_all_appliances():
        rows.append(
            {
                "name": a.name,
                "aliases": ", ".join(a.aliases),
                "is_high_risk": a.is_high_risk,
                "enabled": a.enabled,
                "actions": sorted(a.actions),
                "learned": sum(1 for act in a.actions if act in a.codes),
            }
        )
    return rows


def _index_context(services: UiServices, **extra: object) -> dict[str, object]:
    return {
        "privacy_on": services.privacy.is_on,
        "remote_available": services.remote_available(),
        "network_ok": services.network_ok(),
        "passphrase_set": services.vault.is_set(),
        "volume": services.settings.volume,
        "tts_speed": services.settings.tts_speed,
        "appliances": _appliance_rows(services),
        **extra,
    }


def create_app(services: UiServices) -> FastAPI:
    """构建设备 UI 应用。全部状态经注入的 services 访问（可全面测试）。"""
    app = FastAPI(title="LIRA 设备面板", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", _index_context(services)
        )

    @app.get("/status")
    async def status():
        """只读状态 JSON（状态卡轮询源，不鉴权）。"""
        return JSONResponse(
            {
                "privacy_on": services.privacy.is_on,
                "remote_available": services.remote_available(),
                "network_ok": services.network_ok(),
                "passphrase_set": services.vault.is_set(),
                "volume": services.settings.volume,
                "tts_speed": services.settings.tts_speed,
                "appliance_count": len(services.store.get_all_appliances()),
            }
        )

    # ---------- 口令首启设置（无默认值；模拟屏上操作） ----------

    @app.get("/passphrase/setup")
    async def setup_form(request: Request):
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"already_set": services.vault.is_set(), "error": None},
        )

    @app.post("/passphrase/setup")
    async def setup_submit(request: Request):
        form = await request.form()
        already_set = services.vault.is_set()
        passphrase = str(form.get("passphrase", ""))
        confirm = str(form.get("confirm", ""))
        if already_set:
            # SEC-2：覆盖口令必须先验旧口令（同一 vault.verify 通道，常数时间比较），
            # 否则拿到屏幕/表单的任何人可无凭据重置安全凭据
            if not services.vault.verify(str(form.get("old_passphrase", ""))):
                logger.info("ui event=passphrase_set_blocked reason=old_passphrase_mismatch")
                return templates.TemplateResponse(
                    request,
                    "setup.html",
                    {"already_set": True, "error": "当前口令不正确。"},
                    status_code=403,
                )
        if passphrase != confirm:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {"already_set": already_set, "error": "两次输入不一致。"},
                status_code=400,
            )
        try:
            services.vault.set(passphrase)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {"already_set": already_set, "error": str(exc)},
                status_code=400,
            )
        logger.info("ui event=passphrase_set")
        return RedirectResponse("/", status_code=303)

    # ---------- 隐私开关（UI 通道：需口令；按键通道见 privacy.make_button_toggler） ----------

    @app.post("/privacy")
    async def privacy_toggle(request: Request):
        form = await request.form()
        action = str(form.get("action", ""))
        if action not in ("on", "off"):
            return templates.TemplateResponse(
                request,
                "index.html",
                _index_context(services, privacy_error="操作无效。"),
                status_code=400,
            )
        if not services.vault.is_set():
            # fail-closed：口令未设置 → 先引导设置，绝不无凭据改安全状态
            logger.info("ui event=privacy_blocked reason=passphrase_not_set")
            return RedirectResponse("/passphrase/setup", status_code=303)
        if not services.vault.verify(str(form.get("passphrase", ""))):
            logger.info("ui event=privacy_blocked reason=bad_passphrase")
            return templates.TemplateResponse(
                request,
                "index.html",
                _index_context(services, privacy_error="口令不正确。"),
                status_code=401,
            )
        target = action == "on"
        await services.privacy.set_enabled(target, source="ui")
        # 后果播报由隐私状态订阅者统一发出（与按键路径同源，见 privacy.attach_announcer）
        return RedirectResponse("/", status_code=303)

    # ---------- 音量 / 语速（不涉安全，不设口令） ----------

    async def _apply_setting(request: Request, apply: Callable[[float], None], name: str):
        form = await request.form()
        try:
            value = float(str(form.get("value", "")))
            apply(value)
        except (ValueError, TypeError):
            return templates.TemplateResponse(
                request,
                "index.html",
                _index_context(services, setting_error=f"{name}数值无效。"),
                status_code=400,
            )
        return RedirectResponse("/", status_code=303)

    @app.post("/volume")
    async def set_volume(request: Request):
        return await _apply_setting(request, services.settings.set_volume, "音量")

    @app.post("/speed")
    async def set_speed(request: Request):
        return await _apply_setting(request, services.settings.set_tts_speed, "语速")

    return app


def _demo_services() -> UiServices:  # pragma: no cover - 本地浏览用
    """x86 本地浏览用 mock 装配（内存库，无 HAL 依赖）。"""
    store = ApplianceStore(":memory:")
    return UiServices(
        privacy=PrivacyState(),
        vault=PassphraseVault(store),
        store=store,
        remote_available=lambda: True,
        network_ok=lambda: True,
    )


if __name__ == "__main__":  # pragma: no cover - 手动验证入口
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(_demo_services()), host="127.0.0.1", port=8000)
