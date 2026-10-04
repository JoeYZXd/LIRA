"""camera_isp 多帧合并纯函数主机测试——板级采集路径无硬件不可测，纯函数按仓库惯例直测。"""

import cv2
import numpy as np

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
