"""Preserve small text with bounded, overlapping detector views.

Keep a coarse page pass for complete long lines. Local views supplement missed
small print; all boxes remain in original image coordinates for recognition.
"""
from collections.abc import Callable

import cv2
import numpy as np


def _starts(length: int, side: int, overlap: int) -> list[int]:
    if length <= side:
        return [0]
    intervals = int(np.ceil((length - side) / (side - overlap)))
    return np.linspace(0, length - side, intervals + 1).astype(int).tolist()


def detect_document(
    image: np.ndarray,
    detector: Callable[[np.ndarray], list[np.ndarray]],
    *,
    tile_side: int = 1024,
    overlap: int = 256,
) -> list[np.ndarray]:
    if not 0 <= overlap < tile_side:
        raise ValueError("Expected 0 <= overlap < tile_side")
    if image.size == 0:
        return []
    h, w = image.shape[:2]
    boxes = detector(image)
    if max(h, w) <= tile_side:
        return boxes
    for y in _starts(h, tile_side, overlap):
        for x in _starts(w, tile_side, overlap):
            crop = image[y:y+tile_side, x:x+tile_side]
            for box in detector(crop):
                boxes.append(box + np.float32([x, y]))
    return merge_boxes(boxes)


def merge_boxes(boxes: list[np.ndarray]) -> list[np.ndarray]:
    """Remove contained duplicates and join overlapping horizontal line pieces.

    Never bridge whitespace between columns. Rotated lines only deduplicate by
    polygon containment; seam joins require matching near-horizontal baselines.
    """
    result: list[np.ndarray] = []
    for candidate in sorted(boxes, key=cv2.contourArea, reverse=True):
        candidate = candidate.astype(np.float32).copy()
        i = 0
        while i < len(result):
            other = result[i]
            small_area = min(cv2.contourArea(candidate), cv2.contourArea(other))
            intersection, _ = cv2.intersectConvexConvex(candidate, other)
            if small_area > 0 and intersection / small_area >= 0.8:
                # Larger original box retains its geometry (no seam inflation).
                if cv2.contourArea(other) >= cv2.contourArea(candidate):
                    candidate = other
                result.pop(i)
                i = 0
                continue
            if _same_horizontal_line(candidate, other):
                points = np.concatenate([candidate, other])
                rect = cv2.boxPoints(cv2.minAreaRect(points))
                left = rect[np.argsort(rect[:, 0])[:2]]
                right = rect[np.argsort(rect[:, 0])[2:]]
                left = left[np.argsort(left[:, 1])]
                right = right[np.argsort(right[:, 1])]
                candidate = np.float32([left[0], right[0], right[1], left[1]])
                result.pop(i)
                i = 0
                continue
            i += 1
        result.append(candidate)
    return result


def _same_horizontal_line(a: np.ndarray, b: np.ndarray) -> bool:
    vectors = [p[1] - p[0] for p in (a, b)]
    angles = [np.degrees(np.arctan2(v[1], v[0])) for v in vectors]
    if max(abs(angle) for angle in angles) > 10 or abs(angles[0] - angles[1]) > 3:
        return False
    # Compare across-line separation in baseline coordinates, not the page's
    # axis-aligned bounds: a long tilted line has a misleadingly tall envelope.
    angle = np.radians(sum(angles) / 2)
    axes = np.float32([[np.cos(angle), np.sin(angle)],
                      [-np.sin(angle), np.cos(angle)]])
    projected_a, projected_b = a @ axes.T, b @ axes.T
    ax0, ay0 = projected_a.min(axis=0)
    ax1, ay1 = projected_a.max(axis=0)
    bx0, by0 = projected_b.min(axis=0)
    bx1, by1 = projected_b.max(axis=0)
    ah, bh = ay1-ay0, by1-by0
    if min(ah, bh) <= 0 or max(ah, bh) / min(ah, bh) > 1.7:
        return False
    return bool(
        min(ax1, bx1) > max(ax0, bx0)
        and abs((ay0+ay1)-(by0+by1)) / 2 <= min(ah, bh) * 0.3
    )
