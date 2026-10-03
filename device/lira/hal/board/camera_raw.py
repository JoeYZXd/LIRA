"""板上摄像头 HAL（U10）：v4l2 裸 Bayer 采集 + 用户态解码（无 ISP 路径）。

本镜像 rkcif→rkisp 在线链无 SDITF 实体（U10 实测），ISP 内联不可用；
v1 走已验证的裸帧路径：v4l2-ctl 单帧抓取（SBGGR10，驱动钳宽后 2688x3136）
→ numpy
10bit→8bit（>>8，MSB 对齐）→ cv2 Bayer→BGR → 灰世界白平衡 + gamma → JPEG。

质量注记：无 CCM/gamma/降噪（ISP 职能），白纸黑字 + 环境光场景够用；
若后续镜像补齐 SDITF，可平滑切 ISP mainpath NV12（capture() 接口不变）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import cv2
import numpy as np

from lira.hal.base import Camera, HalError

__all__ = ["V4l2RawCamera"]

DEFAULT_DEVICE = "/dev/video0"
# 2026-10-03 实测：传感器上限 4224 宽，但 rkcif 驱动静默把宽度钳到 2688
# （请求 4224x3136 会协商为 2688x3136；MIPI 带宽/时序约束）
SENSOR_W, SENSOR_H = 2688, 3136
# V4L2 'BG10'（SBGGR10）：10bit MSB 对齐于 16bit 字（低 6 位为 0）→ >>8 得 8bit
_BAYER_CVT = cv2.COLOR_BayerBG2BGR
_CAPTURE_TIMEOUT_S = 10.0
_TMP_RAW = Path("/tmp/lira_capture.raw")


class V4l2RawCamera(Camera):
    """`Camera.capture() -> JPEG bytes` 的板上实现（单帧拍摄语义）。"""

    WB_GAIN_CAP = 2.5
    GAMMA = 1.6

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        width: int = SENSOR_W,
        height: int = SENSOR_H,
    ) -> None:
        self.device = device
        self.width = int(width)
        self.height = int(height)
        self._tmp = _TMP_RAW

    async def open(self) -> None:
        """探测设备可用（也预热传感器时钟）。"""
        if not Path(self.device).exists():
            raise HalError(f"摄像头设备不存在: {self.device}")
        proc = await asyncio.create_subprocess_exec(
            "v4l2-ctl", "-d", self.device, "--get-fmt-video",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), _CAPTURE_TIMEOUT_S)
        if proc.returncode != 0:
            raise HalError(f"v4l2-ctl 探测失败: {stderr.decode(errors='replace').strip()}")

    async def close(self) -> None:
        self._tmp.unlink(missing_ok=True)

    async def capture(self) -> bytes:
        """拍摄一帧：v4l2 单帧抓取 → 解码 → 白平衡/gamma → JPEG。"""
        proc = await asyncio.create_subprocess_exec(
            "v4l2-ctl",
            "-d", self.device,
            "--set-fmt-video=width=%d,height=%d,pixelformat=BG10"
            % (self.width, self.height),
            "--stream-mmap", "--stream-count=1",
            "--stream-to=%s" % self._tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), _CAPTURE_TIMEOUT_S)
        if proc.returncode != 0:
            raise HalError(f"拍摄失败: {stderr.decode(errors='replace').strip()}")

        buf = np.fromfile(self._tmp, dtype="<u2")
        if buf.size != self.width * self.height:
            raise HalError("裸帧大小不符: %d != %dx%d" % (buf.size, self.width, self.height))
        frame8 = (buf.reshape(self.height, self.width) >> 8).astype(np.uint8)
        bgr = cv2.cvtColor(frame8, _BAYER_CVT)
        bgr = _gray_world_wb(bgr, cap=self.WB_GAIN_CAP)
        bgr = _gamma(bgr, self.GAMMA)
        ok, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise HalError("JPEG 编码失败")
        return jpeg.tobytes()


def _gray_world_wb(img: np.ndarray, cap: float = 2.5) -> np.ndarray:
    """灰世界白平衡：三通道均值拉平（增益上限 cap 防极端色偏过矫）。"""
    means = img.reshape(-1, 3).mean(axis=0)
    gain = means.mean() / np.maximum(means, 1.0)
    gain = np.minimum(gain, cap)
    return np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8)


def _gamma(img: np.ndarray, gamma: float) -> np.ndarray:
    """gamma 变换（>1 提亮暗部），LUT 实现。"""
    lut = (np.linspace(0, 1, 256) ** (1.0 / gamma) * 255).astype(np.uint8)
    return cv2.LUT(img, lut)
