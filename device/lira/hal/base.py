"""硬件抽象层（HAL）四接口。

约定（计划 Key Technical Decisions / U1 Approach）：
  - 每个接口是 async 上下文管理器：进入时申请/打开资源，退出时释放。
  - 所有硬件异常统一抛 `HalError`，由上层（状态机）转语音话术，不向上抛裸异常。
  - x86 mock 实现（`lira.hal.mock`）与板上实现（`lira.hal.board`，U10）共存，
    由配置 `hal.backend` 选择。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, Callable

__all__ = [
    "HalError",
    "parse_mac",
    "Camera",
    "AudioIO",
    "IrController",
    "Display",
]


class HalError(Exception):
    """HAL 层统一异常类型（System-Wide Impact：硬件层异常 → 状态机转话术）。"""


def parse_mac(mac: str) -> bytes:
    """MAC 字符串转 6 字节。接受 ``ec:0b:ae:0c:74:e4`` / 连字符 / 裸 12 位十六进制。

    配置校验（config.py）与板上实现（ir_broadlink）共用此唯一实现，规则漂移
    即两侧失配，故上收到 HAL 基础层。
    """
    text = mac.strip().replace(":", "").replace("-", "").lower()
    if len(text) != 12:
        raise HalError(f"MAC {mac!r} 非法（应为 6 字节十六进制，如 ec:0b:ae:0c:74:e4）。")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise HalError(f"MAC {mac!r} 含非十六进制字符。") from exc


class _AsyncResource(ABC):
    """共享的 async 上下文管理器骨架：子类实现 open()/close()。"""

    #: open 失败时的降级工厂（可选资源语义，R9：如 IR 离线回退 Mock，
    # 家电控制不可用但主循环不受影响）；None = 失败即上抛（快速失败可见）。
    fallback_factory: Callable[[], _AsyncResource] | None = None

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

    async def play_stream(self, chunks: AsyncIterable[bytes]) -> None:
        """Consume bounded PCM input and return only after device drain.

        Implementations must override this to provide continuous output; a
        per-chunk play fallback would introduce audible sentence boundaries.
        Board chunks are int16 mono at player.samplerate and must not exceed
        two seconds each. The board bounds queued PCM to two seconds plus one
        in-flight source chunk. Source completion is not output completion.
        """
        raise HalError("此音频接口不支持连续输出")

    async def stop(self) -> None:
        """停止当前输出，返回时硬件已静音；只有输入的实现不支持此操作。"""
        raise HalError("此音频接口不支持停止输出")


class IrController(_AsyncResource, ABC):
    """红外收发接口。板上实现经 BroadLink RM4 Mini（python-broadlink 局域网 UDP
    本地协议；2026-10-03 改道，gpio-ir/ir-ctl/LIRC 路线废弃，见 SETUP.md 2.6）。"""

    @abstractmethod
    async def send(self, code: str) -> None:
        """回放一帧原始码（码值为传输层约定的不透明字符串——板上实现用 base64 载荷）。

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
