"""硬件抽象层：接口定义与实现选择。"""

from lira.hal.base import AudioIO, Camera, Display, HalError, IrController

__all__ = ["AudioIO", "Camera", "Display", "HalError", "IrController"]
