"""真实 WebSocket 同步传输层（U10/M7 接线）：websockets 客户端 → SyncTransport。

设计要点：
  - 后台泵任务把 ``recv()`` 收帧循环入队，``receive()`` 只从队列取——
    SyncClient.run_forever 的 ``asyncio.wait_for(receive(), timeout=心跳)`` 超时
    取消不会丢帧/撕裂半收帧（直接取消底层 recv() 有此风险，e2e SyncPump
    注释记录过该约束）。
  - 帧 = JSON 文本 → dict（与 SyncTransport 协议一致："帧即 JSON-able dict"）。
  - 连接关闭/握手失败统一抛 SyncError，由装配层重连监督循环消化
    （SETUP.md 1.1.1 已知残留「同步循环无重连边界」的装配层兜底）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from lira.sync import SyncError

__all__ = ["WsSyncTransport"]


class WsSyncTransport:
    """``websockets`` 客户端封装：open() 建连，send/receive 语义同 SyncTransport。

    用法::

        transport = WsSyncTransport("ws://<后台IP>:8000/ws/device")
        await transport.open()          # 建连（失败抛 SyncError）
        client = SyncClient(store=..., transport=transport, token=...)
        await client.connect()          # 首帧 hello 出示 token
        await client.run_forever(...)
        await transport.close()
    """

    #: websockets 建连超时（路由器故障/后台停机时快速失败进入重连退避）
    OPEN_TIMEOUT_SECONDS = 10.0
    #: 单帧上限（全量快照含全部家电+已学码值；4MB 足够家庭规模）
    MAX_FRAME_BYTES = 4 * 1024 * 1024

    def __init__(self, url: str) -> None:
        self._url = url
        self._ws = None
        self._queue: asyncio.Queue | None = None
        self._pump: asyncio.Task | None = None

    async def open(self) -> None:
        """建立 WS 连接并启动收帧泵。重复 open 是 no-op（幂等）。"""
        if self._ws is not None:
            return
        try:
            import websockets

            self._ws = await websockets.connect(
                self._url,
                open_timeout=self.OPEN_TIMEOUT_SECONDS,
                ping_interval=20.0,
                ping_timeout=20.0,
                close_timeout=5.0,
                max_size=self.MAX_FRAME_BYTES,
            )
        except Exception as exc:  # noqa: BLE001 - websockets/OSError 异常族折叠为 SyncError
            raise SyncError(f"同步连接建立失败: {exc}") from exc

        self._queue = asyncio.Queue()
        self._pump = asyncio.create_task(self._pump_loop())
        logging.info("同步 WS 已连接: %s", self._url)

    async def _pump_loop(self) -> None:
        """收帧循环：recv → 入队；连接结束（关闭/出错）投递哨兵 None。"""
        try:
            async for frame in self._ws:
                await self._queue.put(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 连接层异常统一按"连接结束"处理
            pass
        finally:
            await self._queue.put(None)  # 哨兵：receive() 转为 SyncError

    async def send(self, frame: dict) -> None:
        """发送一帧 JSON dict（未建连/已断抛 SyncError）。"""
        if self._ws is None:
            raise SyncError("同步连接未建立（先调用 open()）")
        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - websockets 异常族折叠
            raise SyncError(f"同步发送失败: {exc}") from exc

    async def receive(self) -> dict:
        """取下一帧（连接关闭时抛 SyncError；取消安全，不撕裂半收帧）。"""
        if self._ws is None:
            raise SyncError("同步连接未建立（先调用 open()）")
        frame = await self._queue.get()
        if frame is None:
            raise SyncError("同步连接已关闭")
        try:
            return json.loads(frame)
        except (TypeError, ValueError) as exc:
            raise SyncError(f"同步帧非 JSON: {exc}") from exc

    async def close(self) -> None:
        """关闭连接并停泵。幂等；close 后 transport 不可复用（监督循环新建实例）。"""
        if self._ws is None:
            return
        pump, self._pump = self._pump, None
        if pump is not None:
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 - 关闭失败不阻断善后
            pass
        self._ws = None
        self._queue = None
        logging.info("同步 WS 已关闭: %s", self._url)
