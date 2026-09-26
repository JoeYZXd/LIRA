"""设备 WS 连接登记（U8）：在线状态面 + 学习回执路由的登记薄。

职责刻意最小化：跨请求/跨 loop 的帧推送不做（TestClient 下每个请求独立
事件 loop，无法直接跨 loop 发送）——配置推送/学习下发统一由 WS 连接自身
的轮询循环驱动（见 `api.devices.device_ws`），本模块只维护「谁在线」。
"""

from __future__ import annotations

import time
from threading import Lock


class DeviceHub:
    """设备名 → 最近心跳时刻的登记薄（进程内，重启即失、以 last_seen 兜底）。"""

    #: 在线判定：WS 连接登记即在线（登记由连接生命周期保证增删）
    ONLINE_GRACE_SECONDS = 0.0

    def __init__(self) -> None:
        self._lock = Lock()
        self._connected: dict[str, float] = {}

    def connect(self, name: str) -> None:
        with self._lock:
            self._connected[name] = time.time()

    def disconnect(self, name: str) -> None:
        with self._lock:
            self._connected.pop(name, None)

    def is_online(self, name: str) -> bool:
        with self._lock:
            return name in self._connected
