"""M7 真实 WS 同步传输层测试（lira/sync_ws.py，websockets 栈）。

核心约束（e2e SyncPump 注释记录过）：SyncClient.run_forever 的
``asyncio.wait_for(receive(), timeout=心跳)`` 超时取消不得丢帧/撕裂半收帧
——传输层用后台泵 + 队列结构性保证，此处实测验证。
"""

from __future__ import annotations

import asyncio
import json

import pytest

websockets = pytest.importorskip("websockets")

from lira.protocol import HelloMsg  # noqa: E402
from lira.sync import SyncError  # noqa: E402
from lira.sync_ws import WsSyncTransport  # noqa: E402


async def _start_server(handler):
    """起临时 websockets 服务，返回 (server, port)。"""
    from websockets.asyncio.server import serve

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


class TestWsSyncTransport:
    async def test_open_send_receive_roundtrip(self):
        """建连 -> 首帧 hello -> 收 auth_ok 应答（与 SyncClient.connect 语义一致）。"""
        received: list[dict] = []

        async def handler(ws):
            received.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"type": "auth_ok"}))
            await ws.recv()  # 保持连接直到测试收尾

        server, port = await _start_server(handler)
        transport = WsSyncTransport(f"ws://127.0.0.1:{port}")
        try:
            await transport.open()
            await transport.send(HelloMsg(token="tok-1").to_json())
            frame = await transport.receive()
            assert frame == {"type": "auth_ok"}
            assert received == [{"type": "hello", "token": "tok-1"}]
        finally:
            await transport.close()
            server.close()
            await server.wait_closed()

    async def test_receive_cancel_does_not_lose_frame(self):
        """wait_for(receive) 超时取消后再收帧不丢（后台泵结构性保证）。"""
        frames_after = []

        async def handler(ws):
            await ws.recv()  # hello
            await ws.send(json.dumps({"type": "auth_ok"}))
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({"type": "snapshot", "epoch": 1, "version": 6}))
            await asyncio.sleep(5)

        server, port = await _start_server(handler)
        transport = WsSyncTransport(f"ws://127.0.0.1:{port}")
        try:
            await transport.open()
            await transport.send(HelloMsg(token="t").to_json())
            assert (await transport.receive()) == {"type": "auth_ok"}
            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(transport.receive(), timeout=0.02)
            frame = await asyncio.wait_for(transport.receive(), timeout=2.0)
            frames_after.append(frame)
            assert frame["type"] == "snapshot"
        finally:
            await transport.close()
            server.close()
            await server.wait_closed()

    async def test_receive_after_close_raises_syncerror(self):
        """连接关闭后 receive 抛 SyncError（监督循环的重连触发面）。"""

        async def handler(ws):
            await ws.recv()
            await ws.send(json.dumps({"type": "auth_ok"}))

        server, port = await _start_server(handler)
        transport = WsSyncTransport(f"ws://127.0.0.1:{port}")
        try:
            await transport.open()
            await transport.send(HelloMsg(token="t").to_json())
            await transport.receive()
        finally:
            await transport.close()
            server.close()
            await server.wait_closed()
        with pytest.raises(SyncError):
            await transport.receive()

    async def test_double_close_is_noop(self):
        async def handler(ws):
            await ws.recv()

        server, port = await _start_server(handler)
        transport = WsSyncTransport(f"ws://127.0.0.1:{port}")
        await transport.open()
        await transport.send(HelloMsg(token="t").to_json())
        await transport.close()
        await transport.close()  # 幂等
        server.close()
        await server.wait_closed()