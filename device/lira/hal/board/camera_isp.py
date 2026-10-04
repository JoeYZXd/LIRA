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

#: 传感器曝光/增益（板上实测）：增益 128-1984（模拟增益是最有效的亮度杠杆，
#: 也是最大的噪声源——压噪优先拉长曝光而非抬增益）
_EXP_MAX = 6400
_GAIN_DARK = 1984
_GAIN_BRIGHT = 384
_LUMA_LOW = 70.0
_LUMA_HIGH = 190.0

#: 15fps 档：拉高 vblank 换长曝光（vts 6436 ≥ 曝光上限 6400），增益需求约减半；
#: 静态阅读场景吃得下帧率损失（单拍流程，非连续流）
_VBLANK_15FPS = 3300

#: 多帧对齐判定：512 宽下相位相关平均位移超过该像素数视为失配（回退择优）
_ALIGN_MAX_SHIFT = 2.0


class V4l2IspCamera(Camera):
    """`Camera.capture() -> JPEG bytes` 的 RKISP 管线实现。"""

    def __init__(self, device: str = _ISP_DEVICE, rotation: int = 0, hflip: bool = False) -> None:
        self.device = device
        self._tmp = _TMP_NV12
        #: 画面旋转（0/90/180/270 度，顺时针）与水平翻转——装配/挂载方向
        #: 各异，由控制台调整并持久化（store meta）
        self.rotation = rotation
        self.hflip = hflip

    async def open(self) -> None:
        """探测 ISP mainpath 节点，切 15fps 档并把曝光固定到上限。"""
        if not Path(self.device).exists():
            raise HalError(f"ISP mainpath 节点不存在: {self.device}（检查相机 overlay）")
        await self._sensor_set("vertical_blanking", _VBLANK_15FPS)  # 失败则曝光被驱动钳回原上限，功能不损
        await self._sensor_set("exposure", _EXP_MAX)
        self._sensor_gain = 1024  # 自适应起点（暗室起步，亮场景首拍后下调）

    async def close(self) -> None:
        self._tmp.unlink(missing_ok=True)

    async def capture(self) -> bytes:
        """单批三帧 → 对齐均值降噪（固定机位免费 √3 信噪比；失配回退择优）→
        空域降噪 → 数字补偿 → JPEG。"""
        frames = await self._capture_batch_adaptive()
        merged, _aligned = _merge_frames(frames)
        merged = cv2.bilateralFilter(merged, 9, 50, 50)
        best = _digital_exposure(merged)
        ok, jpeg = cv2.imencode(".jpg", best, [cv2.IMWRITE_JPEG_QUALITY, 88])
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

    async def _capture_batch_adaptive(self, n: int = 3) -> list[np.ndarray]:
        """批级自适应采集：暗室起步高增益；整批过暗/过亮按比例调增益重拍一批。
        （一次流抓整批——15fps 长曝光下逐帧起 v4l2-ctl 的进程开销不可接受）"""
        gain = getattr(self, "_sensor_gain", 1024)
        frames = await self._capture_batch(gain, n)
        mean = self._luma_mean(frames[0])
        if mean < _LUMA_LOW and gain < _GAIN_DARK:
            new_gain = min(int(gain * _LUMA_LOW / max(mean, 1.0)), _GAIN_DARK)
            if new_gain > gain:
                await self._sensor_set("analogue_gain", new_gain)
                self._sensor_gain = new_gain
                frames = await self._capture_batch(new_gain, n)
        elif mean > _LUMA_HIGH and gain > _GAIN_BRIGHT:
            new_gain = max(int(gain * _LUMA_HIGH / max(mean, 1.0)), _GAIN_BRIGHT)
            await self._sensor_set("analogue_gain", new_gain)
            self._sensor_gain = new_gain
            frames = await self._capture_batch(new_gain, n)
        return frames

    async def _capture_batch(self, gain: int, n: int) -> list[np.ndarray]:
        """单次 v4l2-ctl 流抓 n 帧（NV12 → BGR，逐帧套用方向配置）。"""
        await self._sensor_set("analogue_gain", gain)
        proc = await asyncio.create_subprocess_exec(
            "v4l2-ctl",
            "-d", self.device,
            "--set-fmt-video=width=2688,height=3136,pixelformat=NV12",
            "--stream-mmap", "--stream-count=%d" % n,
            "--stream-to=%s" % self._tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), _CAPTURE_TIMEOUT_S)
        if proc.returncode != 0:
            raise HalError(f"ISP 拍摄失败: {stderr.decode(errors='replace').strip()}")

        w, h = 2688, 3136
        expected = w * h * 3 // 2
        buf = np.fromfile(self._tmp, dtype=np.uint8)
        got = min(len(buf) // expected, n)
        if got == 0:
            raise HalError("ISP 帧大小不符: %d 字节不足一帧" % len(buf))
        frames = []
        for i in range(got):
            yuv = buf[i * expected : (i + 1) * expected].reshape((h * 3) // 2, w)
            bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
            frames.append(_apply_orientation(bgr, self.rotation, self.hflip))
        return frames


def _sharpness(bgr: np.ndarray) -> float:
    """锐度度量：亮度图拉普拉斯方差（对焦/抖动的经典判据，越大越锐）。"""
    y = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)[..., 0]
    return float(cv2.Laplacian(y, cv2.CV_64F).var())


def _merge_frames(
    frames: list[np.ndarray], max_shift: float = _ALIGN_MAX_SHIFT
) -> tuple[np.ndarray, bool]:
    """多帧合并：相位相关判定对齐（512 宽下平均位移 ≤ max_shift）→ 逐帧平移配准
    后取均值（固定机位下等效 √N 倍信噪比）；失配则回退最锐利单帧（抗抖动）。

    返回 (合并图, 是否走了对齐均值)。
    """
    if len(frames) < 2:
        return frames[0], False
    scale = 512.0 / frames[0].shape[1]
    smalls = [
        cv2.cvtColor(cv2.resize(f, None, fx=scale, fy=scale), cv2.COLOR_BGR2GRAY)
        for f in frames
    ]
    ref = np.float32(smalls[0])
    shifts = []
    for s in smalls[1:]:
        (dx, dy), _ = cv2.phaseCorrelate(ref, np.float32(s))
        shifts.append((dx, dy))
    if max(max(abs(dx), abs(dy)) for dx, dy in shifts) > max_shift:
        return max(frames, key=_sharpness), False
    h, w = frames[0].shape[:2]
    acc = np.zeros_like(frames[0], dtype=np.float32)
    acc += frames[0]
    for f, (dx, dy) in zip(frames[1:], shifts):
        m = np.float32([[1, 0, dx / scale], [0, 1, dy / scale]])
        acc += cv2.warpAffine(f, m, (w, h))
    return (acc / len(frames)).astype(np.uint8), True


def _apply_orientation(bgr: np.ndarray, rotation: int, hflip: bool) -> np.ndarray:
    """按配置旋转/翻转画面（旋转为顺时针角度，限定 0/90/180/270）。"""
    k = (rotation // 90) % 4
    if k:
        bgr = np.rot90(bgr, k=-k)  # np.rot90 逆时针；-k = 顺时针 k*90
    if hflip:
        bgr = np.ascontiguousarray(bgr[:, ::-1])
    return np.ascontiguousarray(bgr)


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
