"""mock 显示：show/clear 记录到内存列表。"""

from __future__ import annotations

from lira.hal.base import Display, HalError


class MockDisplay(Display):
    """内存版屏幕。`shown` 按调用顺序记录每次 show 的内容。"""

    def __init__(self) -> None:
        self.shown: list[str] = []
        self.current: str = ""

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def show(self, lines: str) -> None:
        if not lines:
            raise HalError("show 内容为空（清屏请用 clear()）。")
        self.current = lines
        self.shown.append(lines)

    async def clear(self) -> None:
        self.current = ""
        self.shown.append("<cleared>")
