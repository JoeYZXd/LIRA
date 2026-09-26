"""版面阅读顺序（U4）：det 多边形 → 多栏聚类 → 阅读顺序（计划 U4 Approach）。

算法（确定性、纯几何，可脱离 OCR 单测）：
  1. 每个文本行取外接框与 x 中心；
  2. 按 x 中心升序贪心聚类成"栏"：当前行中心超出当前栏右缘 + 栏间隙阈值
     （`column_gap_ratio` × 版面总宽）即开新栏——同一栏内行的 x 区间相互交叠，
     报纸栏间距通常 < 6% 版宽，而跨栏跳转远大于此；
  3. 栏按平均 x 从左到右排序；栏内行按 y 上缘从上到下排序。
  → 中文报刊"逐栏阅读"顺序（左栏读完读右栏）。

局限（刻意保持最小实现）：横贯整版的通栏大标题会把多栏并成一栏（仍保持
y 序，不丢内容）；竖排文本不在 Phase 1 范围（R1 目标材料为横排报刊/信件/说明书）。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

__all__ = ["sort_reading_order", "columns_of"]

#: 栏间隙阈值 = 版面总宽 × 该比例（Key Decisions: det 480px 长边下的经验值）
DEFAULT_COLUMN_GAP_RATIO = 0.06


def _bbox(poly: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    """四点多边形 → (x_min, x_max, y_min, y_max)。"""
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return min(xs), max(xs), min(ys), max(ys)


def columns_of(
    polygons: Sequence[Sequence[Sequence[float]]],
    column_gap_ratio: float = DEFAULT_COLUMN_GAP_RATIO,
) -> list[list[int]]:
    """把多边形索引聚类为栏（返回列的列表，每列为行索引列表，列内 y 序）。

    Args:
        polygons: det 四点多边形序列（原图坐标）。
        column_gap_ratio: 栏间隙阈值占版面总宽的比例。
    """
    if not polygons:
        return []

    boxes = [_bbox(p) for p in polygons]
    span = max(b[1] for b in boxes) - min(b[0] for b in boxes)
    gap = max(span * column_gap_ratio, 1.0)

    # 按 x 中心升序贪心成栏
    order = sorted(range(len(polygons)), key=lambda i: (boxes[i][0] + boxes[i][1]) / 2.0)
    columns: list[list[int]] = []
    current: list[int] = []
    current_x_max = -np.inf
    for i in order:
        center_x = (boxes[i][0] + boxes[i][1]) / 2.0
        if current and center_x - current_x_max > gap:
            columns.append(current)
            current = []
        current.append(i)
        current_x_max = max(current_x_max, boxes[i][1])
    if current:
        columns.append(current)

    # 列按平均 x 左→右；列内行按 y 上缘上→下（带 0.5 倍行高容差的行分组不必要：
    # 同栏内 det 行天然按行分布，y_min 排序即阅读序）
    columns.sort(key=lambda col: np.mean([(boxes[i][0] + boxes[i][1]) / 2.0 for i in col]))
    for col in columns:
        col.sort(key=lambda i: boxes[i][2])
    return columns


def sort_reading_order(
    polygons: Sequence[Sequence[Sequence[float]]],
    column_gap_ratio: float = DEFAULT_COLUMN_GAP_RATIO,
) -> list[np.ndarray]:
    """返回按阅读顺序（逐栏、栏内自上而下）重排的多边形列表。"""
    columns = columns_of(polygons, column_gap_ratio)
    ordered: list[np.ndarray] = []
    for col in columns:
        for i in col:
            ordered.append(np.asarray(polygons[i], dtype=np.float32))
    return ordered
