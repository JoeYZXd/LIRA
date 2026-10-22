"""camera_isp 多帧合并纯函数主机测试——板级采集路径无硬件不可测，纯函数按仓库惯例直测。"""

import cv2
import numpy as np
import pytest

from lira.hal.board.camera_isp import _merge_frames, _sharpness


def _base_image() -> np.ndarray:
    """带类文字块的基础图（给相位相关与锐度提供结构）。"""
    rng = np.random.default_rng(0)
    img = np.full((200, 260, 3), 110, np.uint8)
    for _ in range(40):
        x, y = int(rng.integers(0, 220)), int(rng.integers(0, 160))
        img[y : y + 8, x : x + 30] = int(rng.integers(30, 220))
    return img


def _noisy(img: np.ndarray, seed: int, sigma: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, sigma, img.shape)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def test_aligned_frames_average_and_cut_noise():
    base = _base_image()
    frames = [_noisy(base, i, 24.0) for i in range(3)]
    merged, aligned = _merge_frames(frames)
    assert aligned is True
    assert merged.shape == frames[0].shape
    assert merged.dtype == np.uint8
    # √3 期望：与无噪真值的差图噪声应显著低于单帧（>20% 削减即视为生效）
    def sigma_vs_truth(im):
        return float((im.astype(np.float32) - base.astype(np.float32)).std())
    assert sigma_vs_truth(merged) < sigma_vs_truth(frames[0]) * 0.8


def test_misaligned_frames_fall_back_to_sharpest():
    base = _base_image()
    shifted = np.roll(base, 12, axis=1)
    blurred = cv2.GaussianBlur(base, (9, 9), 2.5)
    frames = [
        _noisy(blurred, 1, 5.0),  # 原位但模糊：与移位帧相对错位，且结构 LapVar 被抹平
        _noisy(shifted, 2, 2.0),  # 移位、干净、更锐
        _noisy(shifted, 3, 2.0),
    ]
    merged, aligned = _merge_frames(frames)
    assert aligned is False
    # 回退 = 全场最锐利帧（清晰的移位帧），而非模糊的原位帧
    assert merged is max(frames, key=_sharpness)
    assert merged is not frames[0]


def test_single_frame_passthrough():
    f = _noisy(_base_image(), 0, 5.0)
    merged, aligned = _merge_frames([f])
    assert aligned is False
    assert merged is f


def test_bright_colored_page_keeps_printed_colors():
    from lira.hal.board.camera_isp import _digital_exposure

    # A colorful page is not a gray-world calibration target.
    image = np.full((40, 60, 3), (80, 190, 160), dtype=np.uint8)
    image[10:30, 20:40] = (170, 215, 220)  # pale yellow artwork
    assert np.array_equal(_digital_exposure(image), image)


def test_dark_page_brightening_preserves_channel_ratios():
    from lira.hal.board.camera_isp import _digital_exposure

    image = np.full((20, 30, 3), (20, 40, 60), dtype=np.uint8)
    result = _digital_exposure(image)
    pixel = result[0, 0].astype(float)
    assert pixel[0] > 20
    assert abs(pixel[1] / pixel[0] - 2) < 0.05
    assert abs(pixel[2] / pixel[0] - 3) < 0.05


def test_capture_decodes_negotiated_stride_and_frame_boundaries(tmp_path):
    import asyncio
    from lira.hal.board.camera_isp import V4l2IspCamera

    # Driver accepts a different width and pads rows. Keep chroma neutral.
    width, height, stride = 4, 4, 6
    payloads = []
    for luma in (60, 100, 140):
        raw = np.full((height * 3 // 2, stride), 128, dtype=np.uint8)
        raw[:height, :width] = luma
        raw[:, width:] = 255  # padding must never become picture pixels
        payloads.append(raw.tobytes())
    path = tmp_path / "stream.nv12"
    path.write_bytes(payloads[0] * 5 + b"".join(payloads))
    fmt = """Format Video Capture Multiplanar:
Width/Height : 4/4
Pixel Format : 'NV12'
Number of planes : 1
Field : None
Bytes per Line : 6
Size Image : 36
"""
    calls = []

    class Camera(V4l2IspCamera):
        async def _sensor_set(self, ctrl, value):
            return True

        async def _v4l2(self, *args):
            calls.append(args)
            if "--get-fmt-video" in args:
                return fmt.encode(), b"cap dqbuf: bytesused: 36\n" * 8
            # Verbose set-format output is deliberately NOT part of readback.
            return fmt.encode(), b""

    camera = Camera()
    camera._tmp = path
    frames = asyncio.run(camera._capture_batch(1024, 3))
    assert len(frames) == 3
    for frame, payload in zip(frames, payloads):
        packed = np.frombuffer(payload, np.uint8).reshape(6, 6)[:, :4].copy()
        expected = cv2.cvtColor(packed, cv2.COLOR_YUV2BGR_NV12)
        assert frame.shape == (4, 4, 3)
        np.testing.assert_array_equal(frame, expected)
    assert "--get-fmt-video" not in calls[0]
    assert not any(arg.startswith("--set-fmt") for arg in calls[1])


def test_measured_white_balance_keeps_yellow_artwork_yellow():
    from lira.hal.board.camera_isp import _apply_color_gains

    image = np.array([[[60, 100, 50], [40, 100, 60]]], dtype=np.uint8)
    result = _apply_color_gains(image, np.array([100/60, 1, 2], np.float32))
    np.testing.assert_array_equal(result[0, 0], [100, 100, 100])
    assert result[0, 1, 0] < result[0, 1, 1] < result[0, 1, 2]


def test_dark_gain_does_not_clip_bright_paper_details():
    from lira.hal.board.camera_isp import _digital_exposure

    image = np.full((100, 100, 3), 20, np.uint8)
    image[0, 0] = [200, 220, 240]
    image[0, 1] = [180, 200, 220]
    result = _digital_exposure(image)
    assert result[0, 0, 0] < result[0, 0, 1] < result[0, 0, 2]
    assert result[0, 1, 2] < result[0, 0, 2]


@pytest.mark.parametrize("content", ["{", "{}", '{"gains_bgr":"bad","sensor_exposure":3200,"sensor_gain":384}'])
def test_invalid_calibration_is_hal_error(tmp_path, content):
    import asyncio
    from lira.hal.base import HalError
    from lira.hal.board.camera_isp import V4l2IspCamera

    node = tmp_path / "video"
    node.touch()
    camera = V4l2IspCamera(str(node))
    camera.calibration_path = tmp_path / "color.json"
    camera.calibration_path.write_text(content)
    with pytest.raises(HalError, match="校准"):
        asyncio.run(camera.open())


def test_removed_calibration_resets_gains_on_reopen(tmp_path):
    import asyncio
    from lira.hal.board.camera_isp import V4l2IspCamera

    node = tmp_path / "video"
    node.touch()

    class Camera(V4l2IspCamera):
        async def _sensor_set(self, ctrl, value):
            return True

    camera = Camera(str(node))
    camera.calibration_path = tmp_path / "missing.json"
    camera.color_gains = np.array([1.6, 1, 1.7], np.float32)
    asyncio.run(camera.open())
    np.testing.assert_array_equal(camera.color_gains, [1, 1, 1])


def test_adaptive_gain_protects_small_bright_reference_patch():
    import asyncio
    from lira.hal.board.camera_isp import V4l2IspCamera

    image = np.full((100, 100, 3), 20, np.uint8)
    image[:5, :5] = 200
    gains = []

    class Camera(V4l2IspCamera):
        async def _sensor_set(self, ctrl, value):
            return True

        async def _capture_batch(self, gain, n):
            gains.append(gain)
            return [image] * n

    camera = Camera()
    asyncio.run(camera._capture_batch_adaptive())
    assert len(gains) == 2
    assert 1024 < gains[1] <= int(1024 * 235 / 200)


def test_capture_waits_for_sensor_settling(tmp_path):
    import asyncio
    from lira.hal.board.camera_isp import V4l2IspCamera

    fmt = "Width/Height : 4/4\nPixel Format : 'NV12'\nNumber of planes : 1\nField : None\nBytes per Line : 4\nSize Image : 24\n"
    frames = [bytes([16] * 16 + [128] * 8)] * 5 + [bytes([100] * 16 + [128] * 8)] * 3
    camera = V4l2IspCamera()
    camera._tmp = tmp_path / "capture.raw"
    camera._tmp.write_bytes(b"".join(frames))
    async def sensor(*args): return True
    async def ctl(*args):
        if "--get-fmt-video" in args:
            return fmt.encode(), b"cap dqbuf: bytesused: 24\n" * 8
        return b"", b""
    camera._sensor_set = sensor
    camera._v4l2 = ctl
    result = asyncio.run(camera._capture_batch(512, 3))
    assert len(result) == 3
    assert result[0].mean() > 80


def test_scene_gain_limit_prevents_noise_escalation():
    import asyncio
    from lira.hal.board.camera_isp import V4l2IspCamera

    calls = []
    class Camera(V4l2IspCamera):
        async def _capture_batch(self, gain, n):
            calls.append(gain)
            return [np.full((20, 20, 3), 20, np.uint8)] * n
        async def _sensor_set(self, *args): return True
    camera = Camera()
    camera._sensor_gain = 512
    camera._gain_limit = 512
    asyncio.run(camera._capture_batch_adaptive())
    assert calls == [512]
