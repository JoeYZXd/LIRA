"""运行时可变设置（M7 从 ui/app.py 迁出）：音量/语速的跨模块共享状态。

DeviceSettings 被 DeviceCallbacks.speak（TTS 日志/播放）、SyncClient
（tts_settings 注入）、设备 Web UI 表单三方消费，属于编排层状态而非 UI 层——
原放 ui/app.py 迫使 main() 硬导入 fastapi，使 _run_ui 设计的优雅降级
（UI extras 缺失 -> 告警返回）在生产路径不可达（评审 P2）。
"""

from __future__ import annotations

import threading


class DeviceSettings:
    """音量 / TTS 语速的运行时可变设置（线程安全；持久化留待后续单元）。

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
