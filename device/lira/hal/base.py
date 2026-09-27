"""硬件抽象层（HAL）四接口。

约定（计划 Key Technical Decisions / U1 Approach）：
  - 每个接口是 async 上下文管理器：进入时申请/打开资源，退出时释放。
  - 所有硬件异常统一抛 `HalError`，由上层（状态机）转语音话术，不向上抛裸异常。
  - x86 mock 实现（`lira.hal.mock`）与板上实现（`lira.hal.board`，U10）共存，
    由配置 `hal.backend` 选择。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = [
    "HalError",
    "Camera",
    "AudioIO",
    "IrController",
    "Display",
]


class HalError(Exception):
    """HAL 层统一异常类型（System-Wide Impact：硬件层异常 → 状态机转话术）。"""


class _AsyncResource(ABC):
    """共享的 async 上下文管理器骨架：子类实现 open()/close()。"""

    async def open(self) -> None:
        """申请硬件资源。默认无操作。"""

    async def close(self) -> None:
        """释放硬件资源。默认无操作。"""

    async def __aenter__(self) -> "_AsyncResource":
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


class Camera(_AsyncResource, ABC):
    """拍照接口。"""

    @abstractmethod
    async def capture(self) -> bytes:
        """拍摄一帧并返回图像文件内容（JPEG 字节）。"""



class AudioIO(_AsyncResource, ABC):
    """音频输入/输出接口。统一 16kHz 单声道 16bit PCM（Key Decisions）。"""

    @abstractmethod
    async def read_chunk(self, size: int) -> bytes:
        """读取一帧 PCM。

        到达流末尾时返回 b""（仅 mock 文件源会产生；真实麦克风是无限流）。
        """

    @abstractmethod
    async def play(self, data: bytes) -> None:
        """播放一段 PCM 音频，返回即代表播放完成（TTS 完成事件依赖此语义）。"""


class IrController(_AsyncResource, ABC):
    """红外收发接口。板上实现经 `ir-ctl -r/-s` 原始 pulse/space 码（LIRC 弃用）。"""

    @abstractmethod
    async def send(self, code: str) -> None:
        """回放一帧原始码（pulse/space 文本，与 ir-ctl -s 输入格式一致）。

        前置安全校验（enabled/high-risk）在 appliances 层完成，HAL 层不做业务判断。
        """

    @abstractmethod
    async def learn(self, timeout_seconds: float = 10.0) -> str:
        """学习：录制一帧原始码并返回（对应后台发起的学习流程，R32）。"""


class Display(_AsyncResource, ABC):
    """7 寸触摸屏显示接口（文本层；Web UI 见 lira/ui，U7）。"""

    @abstractmethod
    async def show(self, lines: str) -> None:
        """显示多行文本（换行分隔）。"""

    @abstractmethod
    async def clear(self) -> None:
        """清屏。"""
