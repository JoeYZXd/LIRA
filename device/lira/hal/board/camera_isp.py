"""Board camera: full sensor NV12, aligned denoising and color-preserving gain.

The 4224x3136 mainpath keeps document proportions. Always decode the layout
reported by the driver rather than assuming the requested size was accepted.
Hardware 3A is unavailable on this image; dark captures receive bounded gain.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import json
import re
from pathlib import Path

import cv2
import numpy as np

from lira.hal.base import Camera, HalError
from lira.hal.board.nv12 import decode_nv12, parse_format, split_frames

__all__ = ["V4l2IspCamera"]

_ISP_DEVICE = "/dev/video11"
_SUBDEV_GLOB = "/dev/v4l-subdev*"
_CAPTURE_TIMEOUT_S = 15.0
_TRANSIENT = contextvars.ContextVar("transient_camera", default=False)
_TMP_NV12 = Path("/tmp/lira_isp_frame.raw")
_SENSOR_SUBDEV = "/dev/v4l-subdev2"  # M3：OV13855@CAM1 的传感器子设备

#: 数字曝光目标：帧均值亮度锚定（0-255）。模拟增益打底后的轻量数字补偿。
_TARGET_LUMA = 120.0
_GAIN_CAP = 3.0
_WARMUP_FRAMES = 5

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
        self.color_gains = np.ones(3, dtype=np.float32)
        self.calibration_path = Path(__file__).resolve().parents[3] / "data/camera-color.json"

    async def open(self) -> None:
        """探测 ISP mainpath 节点，切 15fps 档并把曝光固定到上限。"""
        if not Path(self.device).exists():
            raise HalError(f"ISP mainpath 节点不存在: {self.device}（检查相机 overlay）")
        exposure, gain = _EXP_MAX, 1024
        self.color_gains = np.ones(3, dtype=np.float32)
        self._gain_limit = _GAIN_DARK
        try:
            try:
                calibration = json.loads(self.calibration_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                calibration = None
            if calibration is not None:
                gains = np.asarray(calibration["gains_bgr"], dtype=np.float32)
                if gains.shape != (3,) or not np.isfinite(gains).all() or np.any((gains < 0.25) | (gains > 4)):
                    raise ValueError("Invalid white-reference gains")
                exposure, gain = int(calibration["sensor_exposure"]), int(calibration["sensor_gain"])
                if not 4 <= exposure <= _EXP_MAX or not 128 <= gain <= _GAIN_DARK:
                    raise ValueError("Invalid calibration sensor controls")
                gain_limit = int(calibration.get("sensor_gain_limit", _GAIN_DARK))
                if not 128 <= gain <= gain_limit <= _GAIN_DARK:
                    raise ValueError("Invalid calibration gain limit")
                self._gain_limit = gain_limit
                self.color_gains = gains
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HalError("白平衡校准文件无效或不可读") from exc
        await self._sensor_set("vertical_blanking", _VBLANK_15FPS)  # 失败则曝光被驱动钳回原上限，功能不损
        await self._sensor_set("exposure", exposure)
        self._sensor_gain = gain

    async def close(self) -> None:
        self._tmp.unlink(missing_ok=True)

    async def capture_transient(self) -> bytes:
        """Capture for transient OCR, deleting this HAL's raw frame on every exit.

        Existing classic callers keep using capture(). The caller must retain
        exclusive camera ownership until this coroutine actually finishes.
        """
        cancelled = threading.Event()
        guard = threading.Lock()
        running = {}

        def run():
            async def capture_owned():
                token = _TRANSIENT.set(True)
                with guard:
                    running.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
                    if cancelled.is_set():
                        asyncio.current_task().cancel()
                try:
                    return await self.capture()
                finally:
                    # Child processes have been reaped by the transient command
                    # helper before raw data can be deleted or ownership released.
                    self._tmp.unlink(missing_ok=True)
                    _TRANSIENT.reset(token)
                    with guard:
                        running.clear()
            return asyncio.run(capture_owned())

        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            with guard:
                if running:
                    running['loop'].call_soon_threadsafe(running['task'].cancel)
            # Cancellation cannot stop CPU/native work. Keep ownership until the
            # thread completes and its subprocess cleanup really finishes.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Retrieve a late failure without replacing the caller's cancellation.
            if not worker.cancelled():
                worker.exception()
            raise

    async def capture(self) -> bytes:
        """单批三帧 → 对齐均值降噪（固定机位免费 √3 信噪比；失配回退择优）→
        空域降噪 → 数字补偿 → JPEG。"""
        frames = await self._capture_batch_adaptive()
        merged, _aligned = _merge_frames(frames)
        merged = cv2.bilateralFilter(merged, 9, 50, 50)
        balanced = _apply_color_gains(merged, self.color_gains)
        best = _digital_exposure(balanced)
        ok, jpeg = cv2.imencode(".jpg", best, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise HalError("JPEG 编码失败")
        return jpeg.tobytes()

    async def _sensor_set(self, ctrl: str, value: int) -> bool:
        if _TRANSIENT.get():
            code, _, _ = await self._transient_command(
                _SENSOR_SUBDEV, "-c", "%s=%d" % (ctrl, value))
            return code == 0
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
        gain_limit = getattr(self, "_gain_limit", _GAIN_DARK)
        if mean < _LUMA_LOW and gain < gain_limit:
            new_gain = min(
                int(gain * _LUMA_LOW / max(mean, 1.0)), gain_limit,
                int(gain * 235.0 / max(float(frames[0].max()), 1.0)),
            )
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
        # Verbose set-format also prints a layout. Keep it separate so the
        # readback used for decoding is unambiguous.
        await self._v4l2("--set-fmt-video=width=4224,height=3136,pixelformat=NV12")
        stdout, stderr = await self._v4l2(
            "--get-fmt-video", "--verbose", "--stream-mmap",
            "--stream-count=%d" % (n + _WARMUP_FRAMES), "--stream-to=%s" % self._tmp,
        )
        log = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        try:
            fmt = parse_format(log)
            sizes = [int(x) for x in re.findall(r"cap dqbuf:.*?bytesused:\s*(\d+)", log)]
            payloads = split_frames(self._tmp.read_bytes(), sizes, n + _WARMUP_FRAMES)[_WARMUP_FRAMES:]
            return [
                _apply_orientation(decode_nv12(payload, fmt), self.rotation, self.hflip)
                for payload in payloads
            ]
        except ValueError as exc:
            raise HalError(f"ISP 帧布局不符: {exc}") from exc

    async def _v4l2(self, *args: str) -> tuple[bytes, bytes]:
        if _TRANSIENT.get():
            code, stdout, stderr = await self._transient_command(self.device, *args)
            if code != 0:
                raise HalError("ISP transient capture command failed")
            return stdout, stderr
        proc = await asyncio.create_subprocess_exec(
            "v4l2-ctl", "-d", self.device, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), _CAPTURE_TIMEOUT_S)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        if proc.returncode != 0:
            raise HalError(f"ISP 拍摄失败: {stderr.decode(errors='replace').strip()}")
        return stdout, stderr


    async def _transient_command(self, device, *args):
        # Shield process creation too: cancellation can race with child spawn.
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            "v4l2-ctl", "-d", device, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
        proc = None
        try:
            proc = await asyncio.shield(spawning)
            output = await asyncio.wait_for(proc.communicate(), _CAPTURE_TIMEOUT_S)
            return proc.returncode, *output
        except BaseException:
            if proc is None:
                proc = await spawning
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass  # The child exited between returncode check and kill.
            await proc.communicate()
            raise



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
    """Only brighten dark inputs; preserve printed colors and bright-page contrast.

    Colorful books do not satisfy gray-world assumptions. Applying levels,
    gamma and amplified chroma to an already bright page washes out its ink.
    A shared per-pixel BGR gain preserves hue, with smooth highlight roll-off.
    """
    mean = float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean())
    if mean >= _LUMA_LOW:
        return bgr
    gain = min(_TARGET_LUMA / max(mean, 1.0), _GAIN_CAP)
    # Monotonic highlight roll-off keeps whites bounded while lifting shadows.
    # Scale all channels of each pixel equally to retain hue.
    peak = bgr.max(axis=2).astype(np.float32)
    factors = gain / (1.0 + (gain - 1.0) * peak / 255.0)
    adjusted = bgr.astype(np.float32)
    adjusted *= factors[..., None]
    np.rint(adjusted, out=adjusted)
    np.clip(adjusted, 0, 255, out=adjusted)
    return adjusted.astype(np.uint8)


def _apply_color_gains(bgr: np.ndarray, gains: np.ndarray) -> np.ndarray:
    """Apply measured white-reference gains; never infer them from page colors."""
    if np.array_equal(gains, np.ones(3, dtype=np.float32)):
        return bgr
    adjusted = bgr.astype(np.float32)
    adjusted *= gains
    np.clip(adjusted, 0, 255, out=adjusted)
    return adjusted.astype(np.uint8)
