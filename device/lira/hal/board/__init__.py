"""板上 HAL 实现（U10）：真实硬件后端（`hal.backend=board` 时装配）。

当前交付：`V4l2RawCamera`（裸 Bayer 采集）。IrController 板上实现待 M5
（BroadLink 到货后接 `ir_broadlink`）；Display 无触摸屏 → 沿用 MockDisplay
（no-op 语义一致）。
"""

from lira.hal.board.camera_raw import V4l2RawCamera

__all__ = ["V4l2RawCamera"]
