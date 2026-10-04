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
import logging
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
        """探测设备可用（也预热传感器时钟），并把传感器曝光固定到上限。

        曝光上限（3210 行 ≈ 满帧时）：静物/文件拍摄无运动模糊顾虑，室内光
        下最大化信噪比。传感器曝光控制在 /dev/v4l-subdev*（raw CIF 视频节点
        无控制项）；逐个尝试、首个成功者生效——best-effort，失败不阻断。
        """
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
        # 传感器曝光固定到上限（best-effort：raw CIF 节点无控制项，逐个子设备
        # 尝试；室内光下最大化信噪比，静物/文件拍摄无运动模糊顾虑）
        for subdev in sorted(Path("/dev").glob("v4l-subdev*")):
            proc = await asyncio.create_subprocess_exec(
                "v4l2-ctl", "-d", str(subdev), "-c", "exposure=3210",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await proc.wait()
            if rc == 0:
                logging.info("camera exposure set to 3210 via %s", subdev)
                break

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
        frame8 = _stripe_suppress(frame8)
        bgr = cv2.cvtColor(frame8, _BAYER_CVT)
        bgr = _stripe_soften(bgr)
        bgr = _gray_world_wb(bgr, cap=self.WB_GAIN_CAP)
        bgr = _chroma_denoise(bgr)
        bgr = _auto_levels(bgr)
        bgr = _gamma(bgr, self.GAMMA)
        ok, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise HalError("JPEG 编码失败")
        return jpeg.tobytes()


def _stripe_suppress(frame8: np.ndarray, k: int = 9) -> np.ndarray:
    """Bayer 域条纹抑制：各子通道列剖面的**高通部分**（周期性条纹模板）
    逐列扣除。板上实测条纹为固定图案（双帧列剖面相关 1.0、主周期 ~2.5px、
    幅度大）——高通扣除可压掉其中列周期分量；残余二维分量由 _stripe_soften
    的 2D 低通继续压制。行中位数对竖直物体稳健（细物体占少数行）。
    """
    f = frame8.astype(np.float32)
    out = np.empty_like(f)
    for i in range(2):
        for j in range(2):
            sub = f[i::2, j::2]
            prof = np.median(sub, axis=0)
            smooth = np.convolve(prof, np.ones(k) / k, mode="same")
            hp = prof - smooth
            out[i::2, j::2] = np.clip(sub - hp[None, :], 0, 255)
    return out.astype(np.uint8)


def _stripe_soften(bgr: np.ndarray) -> np.ndarray:
    """2D 低通压残余二维周期伪影 + 反锐化恢复边缘（裸读出伪影的抑制组合）。"""
    smooth = cv2.GaussianBlur(bgr, (0, 0), 1.5)
    denoised = cv2.addWeighted(bgr, 0.25, smooth, 0.75, 0)
    return cv2.addWeighted(denoised, 1.6, cv2.GaussianBlur(denoised, (0, 0), 2.0), -0.6, 0)


def _chroma_denoise(bgr: np.ndarray, ksize: int = 5) -> np.ndarray:
    """色度去噪：YCrCb 的 Cr/Cb 中值滤波（高增益彩色斑点噪声的主战场，
    亮度细节不动）。"""
    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycrcb)
    cr = cv2.medianBlur(cr, ksize)
    cb = cv2.medianBlur(cb, ksize)
    return cv2.cvtColor(cv2.merge((y, cr, cb)), cv2.COLOR_YCrCb2BGR)


def _auto_levels(bgr: np.ndarray, low_pct: float = 2.0, high_pct: float = 99.0) -> np.ndarray:
    """自动对比度：按亮度百分位拉伸（洗白/低对比裸帧增强），色度同步
    缩放（围绕 128）。平坦帧（hi-lo < 10）原样返回。"""
    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    y = ycrcb[..., 0].astype(np.float32)
    lo, hi = np.percentile(y, (low_pct, high_pct))
    if hi - lo < 10:
        return bgr
    scale = 255.0 / (hi - lo)
    y2 = np.clip((y - lo) * scale, 0, 255)
    cr = np.clip((ycrcb[..., 1].astype(np.float32) - 128.0) * scale + 128.0, 0, 255)
    cb = np.clip((ycrcb[..., 2].astype(np.float32) - 128.0) * scale + 128.0, 0, 255)
    return cv2.cvtColor(
        cv2.merge((y2.astype(np.uint8), cr.astype(np.uint8), cb.astype(np.uint8))),
        cv2.COLOR_YCrCb2BGR,
    )


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
