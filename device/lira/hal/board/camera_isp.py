"""板上相机 HAL（U10/M8）：RKISP 硬件管线（NV12 mainpath）→ 数字曝光补偿。

取代裸 Bayer 路径（camera_raw.py）：板上实测裸路径满幅列条纹（MIPI 读出
伪影，软件不可根治），而 **RKISP 管线内核默认链路已通**——/dev/media1
(rkisp0-vir2) → rkisp_mainpath (/dev/video11) 直接输出 NV12，画面完全
干净、细节锐利。

曝光/增益说明：3A 需 rkaiq 守护进程（闭源，本镜像未带且无公开 deb），
本实现以**数字曝光补偿**替代——按帧均值锚定目标亮度做 levels+gamma，
干净帧上大力提亮可用。若后续装上 rkaiq（ispserver），可将补偿系数调小。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import cv2
import numpy as np

from lira.hal.base import Camera, HalError

__all__ = ["V4l2IspCamera"]

_ISP_DEVICE = "/dev/video11"
_SUBDEV_GLOB = "/dev/v4l-subdev*"
_CAPTURE_TIMEOUT_S = 15.0
_TMP_NV12 = Path("/tmp/lira_isp_frame.raw")
_SENSOR_SUBDEV = "/dev/v4l-subdev2"  # M3：OV13855@CAM1 的传感器子设备

#: 数字曝光目标：帧均值亮度锚定（0-255）。模拟增益打底后的轻量数字补偿。
_TARGET_LUMA = 120.0
_GAMMA = 2.0
_GAIN_CAP = 3.0

#: 传感器曝光/增益（板上实测）：曝光上限 3210 行 ≈ 1 帧时；增益 128-1984
#: （128 时室内均值 ~5，1984 时 ~72——模拟增益是最有效的亮度杠杆）
_EXP_MAX = 3210
_GAIN_DARK = 1984
_GAIN_BRIGHT = 384
_LUMA_LOW = 70.0
_LUMA_HIGH = 190.0


class V4l2IspCamera(Camera):
    """`Camera.capture() -> JPEG bytes` 的 RKISP 管线实现。"""

    def __init__(self, device: str = _ISP_DEVICE) -> None:
        self.device = device
        self._tmp = _TMP_NV12

    async def open(self) -> None:
        """探测 ISP mainpath 节点，并把传感器曝光固定到上限。"""
        if not Path(self.device).exists():
            raise HalError(f"ISP mainpath 节点不存在: {self.device}（检查相机 overlay）")
        await self._sensor_set("exposure", _EXP_MAX)
        self._sensor_gain = 1024  # 自适应起点（暗室起步，亮场景首拍后下调）

    async def close(self) -> None:
        self._tmp.unlink(missing_ok=True)

    async def capture(self) -> bytes:
        """抓 NV12 → BGR → 简易软件 3A（模拟增益自适应）→ 数字补偿 → JPEG。"""
        bgr = await self._capture_bgr_adaptive()
        bgr = _digital_exposure(bgr)
        ok, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise HalError("JPEG 编码失败")
        return jpeg.tobytes()

    async def _sensor_set(self, ctrl: str, value: int) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "v4l2-ctl", "-d", _SENSOR_SUBDEV, "-c", "%s=%d" % (ctrl, value),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return (await proc.wait()) == 0

    def _luma_mean(self, bgr: np.ndarray) -> float:
        return float(cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)[..., 0].mean())

    async def _capture_bgr_adaptive(self) -> np.ndarray:
        """单次自适应采集：暗室起步高增益；过暗/过亮按比例调增益重拍一次。"""
        gain = getattr(self, "_sensor_gain", 1024)
        bgr = await self._capture_bgr(gain)
        mean = self._luma_mean(bgr)
        if mean < _LUMA_LOW and gain < _GAIN_DARK:
            new_gain = min(int(gain * _LUMA_LOW / max(mean, 1.0)), _GAIN_DARK)
            if new_gain > gain:
                await self._sensor_set("analogue_gain", new_gain)
                self._sensor_gain = new_gain
                bgr = await self._capture_bgr(new_gain)
                mean = self._luma_mean(bgr)
        elif mean > _LUMA_HIGH and gain > _GAIN_BRIGHT:
            new_gain = max(int(gain * _LUMA_HIGH / max(mean, 1.0)), _GAIN_BRIGHT)
            await self._sensor_set("analogue_gain", new_gain)
            self._sensor_gain = new_gain
            bgr = await self._capture_bgr(new_gain)
        return bgr

    async def _capture_bgr(self, gain: int) -> np.ndarray:
        await self._sensor_set("analogue_gain", gain)
        proc = await asyncio.create_subprocess_exec(
            "v4l2-ctl",
            "-d", self.device,
            "--set-fmt-video=width=2688,height=3136,pixelformat=NV12",
            "--stream-mmap", "--stream-count=1",
            "--stream-to=%s" % self._tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), _CAPTURE_TIMEOUT_S)
        if proc.returncode != 0:
            raise HalError(f"ISP 拍摄失败: {stderr.decode(errors='replace').strip()}")

        w, h = 2688, 3136
        expected = w * h * 3 // 2
        buf = np.fromfile(self._tmp, dtype=np.uint8, count=expected)
        if buf.size != expected:
            raise HalError("ISP 帧大小不符: %d != %d" % (buf.size, expected))
        yuv = buf.reshape((h * 3) // 2, w)
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)


def _digital_exposure(bgr: np.ndarray) -> np.ndarray:
    """数字曝光补偿：灰世界白平衡（去 LED 绿偏）+ 均值锚定 + levels + gamma。

    干净帧（ISP 去马赛克/降噪为硬件质量）上大幅提亮不会像裸帧那样放大
    条纹；过曝保护用 99.5 分位做白点钳制。
    """
    from lira.hal.board.camera_raw import _gray_world_wb

    bgr = _gray_world_wb(bgr, cap=2.5)
    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    y = ycrcb[..., 0].astype(np.float32)
    mean = float(y.mean())
    gain = min(max(_TARGET_LUMA / max(mean, 1.0), 1.0), _GAIN_CAP)
    y2 = y * gain
    lo, hi = np.percentile(y2, (0.5, 99.5))
    if hi - lo >= 10:
        y2 = np.clip((y2 - lo) * (255.0 / (hi - lo)), 0, 255)
    lut = (np.linspace(0, 1, 256) ** (1.0 / _GAMMA) * 255).astype(np.uint8)
    y2 = cv2.LUT(y2.astype(np.uint8), lut)
    ycrcb[..., 0] = y2
    # 色度随亮度增益轻提（欠曝帧色彩偏暗；钳制防过饱和）
    for ch in (1, 2):
        chf = (ycrcb[..., ch].astype(np.float32) - 128.0) * min(gain, 3.0) + 128.0
        ycrcb[..., ch] = np.clip(chf, 0, 255)
    return cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
