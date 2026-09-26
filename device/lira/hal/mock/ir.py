"""mock 红外：send 只记录码值到内存列表（U1 测试场景 3）；learn 从预置队列注入。

板上实现（U10）将替换为 `ir-ctl -r/-s` 子进程调用。
"""

from __future__ import annotations

import asyncio

from lira.hal.base import HalError, IrController


class MockIrController(IrController):
    """内存版红外控制器。

    Attributes:
        sent_codes: 已发送码值列表（send 顺序记录）。
        learn_queue: 预置"学习结果"队列，learn() 逐个弹出；为空则按超时失败。
    """

    def __init__(self) -> None:
        self.sent_codes: list[str] = []
        self.learn_queue: list[str] = []
        self.send_count = 0

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def send(self, code: str) -> None:
        if not code:
            raise HalError("send 码值为空，拒绝发送。")
        self.sent_codes.append(code)
        self.send_count += 1

    async def learn(self, timeout_seconds: float = 10.0) -> str:
        if not self.learn_queue:
            await asyncio.sleep(min(timeout_seconds, 0.01))  # 模拟录制等待
            raise HalError(f"mock 红外学习超时（{timeout_seconds}s）：learn_queue 为空。")
        code = self.learn_queue.pop(0)
        self.sent_codes.append(f"<learned>{code}")  # 学习到的码也走日志记录通道
        return code
