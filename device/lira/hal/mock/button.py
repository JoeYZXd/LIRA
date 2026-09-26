"""mock 物理按键：程序化触发回调（测试/键盘模拟，R19 隐私退出通道）。"""

from __future__ import annotations

import asyncio
import inspect

from lira.hal.base import Button, ButtonCallback


class MockButton(Button):
    """`press()` 模拟一次物理按键；回调支持同步与 async 函数。"""

    def __init__(self) -> None:
        self._callback: ButtonCallback | None = None
        self.press_count = 0

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def on_press(self, callback: ButtonCallback) -> None:
        self._callback = callback

    def press(self) -> None:
        """模拟一次按键事件。"""
        self.press_count += 1
        if self._callback is None:
            return
        result = self._callback()
        if inspect.isawaitable(result):
            asyncio.get_event_loop().create_task(result)
