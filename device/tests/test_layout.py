"""U4 版面排序测试（计划 Test scenarios：单栏顺序正确 / 三栏报纸按列分组）。

纯几何算法：直接构造 det 多边形输入（确定性），不依赖 OCR 模型。
"""

from __future__ import annotations

import numpy as np
import pytest

from lira.vision.layout import columns_of, sort_reading_order


def box(x: float, y: float, w: float = 200.0, h: float = 40.0) -> np.ndarray:
    """tl 起点的水平文本行四边形（tl→tr→br→bl）。"""
    return np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32
    )


# ---------- 单栏：栏内按 y 自上而下 ----------


class TestSingleColumn:
    def test_stacked_lines_ordered_top_down(self):
        """Test scenario 1（几何半边）: 单栏信件行序 = y 升序，即使 det 输出乱序。"""
        polys = [box(100, 300), box(100, 100), box(100, 200)]
        ordered = sort_reading_order(polys)
        tops = [float(p[0][1]) for p in ordered]
        assert tops == [100.0, 200.0, 300.0]

    def test_x_jitter_stays_same_column(self):
        """行间轻微 x 抖动（拍摄倾斜）不得被拆成多栏。"""
        polys = [box(100 + jitter, 100 + i * 50) for i, jitter in enumerate((0, 8, -6))]
        columns = columns_of(polys)
        assert len(columns) == 1
        assert columns[0] == [0, 1, 2]

    def test_reading_order_preserves_input_values(self):
        polys = [box(50, 0), box(50, 60)]
        ordered = sort_reading_order(polys)
        np.testing.assert_array_equal(ordered[0], box(50, 0))
        np.testing.assert_array_equal(ordered[1], box(50, 60))


# ---------- 三栏报纸：左栏读完读中栏、右栏 ----------


class TestThreeColumns:
    def test_three_columns_grouped_left_to_right(self):
        """Test scenario 2（几何半边）: 3×3 栏 → 逐列分组，列内自上而下。"""
        col_x = (60.0, 460.0, 860.0)
        polys = []
        for cx in col_x:
            for row in range(3):
                polys.append(box(cx, 60 + row * 90))
        # 打乱输入（模拟 det 任意输出顺序）
        shuffled = [polys[i] for i in (4, 0, 8, 2, 6, 1, 7, 3, 5)]

        columns = columns_of(shuffled)
        assert len(columns) == 3
        ordered = sort_reading_order(shuffled)
        lefts = [float(p[0][0]) for p in ordered]
        # 左列 3 行 → 中列 3 行 → 右列 3 行
        assert lefts == [60.0] * 3 + [460.0] * 3 + [860.0] * 3
        for start in (0, 3, 6):
            tops = [float(p[0][1]) for p in ordered[start:start + 3]]
            assert tops == sorted(tops), "栏内应按 y 自上而下"

    def test_two_full_width_rows_is_one_column(self):
        """通栏两行（非多栏）→ 单栏 y 序，不得按 x 误拆。"""
        polys = [box(50, 0, w=900), box(50, 60, w=900)]
        assert len(columns_of(polys)) == 1

    def test_uneven_column_heights(self):
        """栏高不齐（左 4 行右 2 行）仍逐栏读完。"""
        left = [box(60, 60 + i * 50) for i in range(4)]
        right = [box(460, 60 + i * 50) for i in range(2)]
        ordered = sort_reading_order(left + right)
        xs = [float(p[0][0]) for p in ordered]
        assert xs == [60.0] * 4 + [460.0] * 2


# ---------- 边界 ----------


class TestEdges:
    def test_empty_input(self):
        assert sort_reading_order([]) == []
        assert columns_of([]) == []

    def test_single_line(self):
        ordered = sort_reading_order([box(10, 10)])
        assert len(ordered) == 1
