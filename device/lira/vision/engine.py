"""OCR 引擎接口（U4）：检测 + 识别的抽象，x86/板上双后端共用（Key Decisions「OCR 双后端」）。

约定：
  - 图像一律为 BGR `np.ndarray`（与 OpenCV 读取/板上解码约定一致）。
  - `detect` 返回文本行四点多边形（原图坐标，tl→tr→br→bl 顺时针）；
  - `recognize` 接收裁剪后的文本行小图，返回识别文本；
  - `read` 为业务入口：detect → 版面排序（layout）→ 裁剪 → 识别 → `OcrLine` 列表。
    板上 .rknn 后端（U10）只需实现 detect/recognize，输入输出约定与此对齐
    （det 长边 480px 限制，Key Decisions）。

隐私纪律：识别文本不落日志（计划「日志纪律」），本模块只记录检测行数与耗时。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

import cv2
import numpy as np

from lira.vision.layout import sort_reading_order

__all__ = ["OcrLine", "OcrEngine", "crop_rotated_box"]


@dataclass(frozen=True)
class OcrLine:
    """一行识别结果：四点多边形（原图坐标）+ 文本。"""

    polygon: tuple[tuple[float, float], ...]
    text: str

    @property
    def x_min(self) -> float:
        return min(p[0] for p in self.polygon)

    @property
    def x_max(self) -> float:
        return max(p[0] for p in self.polygon)

    @property
    def y_min(self) -> float:
        return min(p[1] for p in self.polygon)

    @property
    def y_max(self) -> float:
        return max(p[1] for p in self.polygon)

    @property
    def center_x(self) -> float:
        return (self.x_min + self.x_max) / 2.0

    @property
    def height(self) -> float:
        return self.y_max - self.y_min


class OcrEngine(ABC):
    """OCR 双后端接口：x86 onnxruntime（本单元）与板上 RKNNLite（U10）。"""

    @abstractmethod
    def detect(self, image: np.ndarray) -> list[np.ndarray]:
        """检测文本行，返回四点多边形列表（原图坐标，float32 (4,2)，tl→tr→br→bl）。"""

    @abstractmethod
    def recognize(self, crop: np.ndarray) -> str:
        """识别单行文本小图（BGR），返回文本（无内容时返回空串）。"""

    def read(self, image: np.ndarray) -> list[OcrLine]:
        """业务入口：检测 → 阅读顺序排序 → 逐行识别。空白/糊图返回空列表。"""
        polygons = self.detect(image)
        ordered = sort_reading_order(polygons)
        lines: list[OcrLine] = []
        for poly in ordered:
            crop = crop_rotated_box(image, poly)
            text = self.recognize(crop).strip()
            if text:
                lines.append(OcrLine(polygon=tuple(map(tuple, poly)), text=text))
        return lines


def crop_rotated_box(image: np.ndarray, polygon: Sequence[Sequence[float]]) -> np.ndarray:
    """透视裁剪一个（可能带旋转角的）四点文本框 → 水平小图（PaddleOCR get_rotate_crop_image）。

    输出高度 = 短边，宽度 = 长边，保持文字水平可读（向左倾斜补偿）。
    """
    points = np.float32(polygon)
    width = int(max(np.linalg.norm(points[0] - points[1]), np.linalg.norm(points[2] - points[3])))
    height = int(max(np.linalg.norm(points[0] - points[3]), np.linalg.norm(points[1] - points[2])))
    if width <= 0 or height <= 0:
        return np.empty((0, 0, 3), dtype=np.uint8)
    dst = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    matrix = cv2.getPerspectiveTransform(points, dst)
    return cv2.warpPerspective(
        image, matrix, (width, height),
        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
    )
