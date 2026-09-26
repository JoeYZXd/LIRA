"""x86 开发 mock HAL 实现（计划 Output Structure：假摄像头目录/文件音频/日志红外）。"""

from lira.hal.mock.audio import MockAudioIO
from lira.hal.mock.button import MockButton
from lira.hal.mock.camera import IMAGE_SUFFIXES, MockCamera
from lira.hal.mock.display import MockDisplay
from lira.hal.mock.ir import MockIrController

__all__ = [
    "IMAGE_SUFFIXES",
    "MockAudioIO",
    "MockButton",
    "MockCamera",
    "MockDisplay",
    "MockIrController",
]
