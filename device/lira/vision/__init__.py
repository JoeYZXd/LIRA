"""视觉/OCR 阅读管线（U4）：engine 抽象、x86 onnx 后端、多栏排序、阅读会话。

板上 .rknn 后端（U10）实现 `engine.OcrEngine` 接口即可替换 x86 实现。
"""

from lira.vision.engine import OcrEngine, OcrLine
from lira.vision.layout import columns_of, sort_reading_order
from lira.vision.reading import (
    BlockSpeaker,
    ReadingPipeline,
    ReadingSession,
    TtsBlockSpeaker,
    chunk_lines,
)

__all__ = [
    "OcrEngine",
    "OcrLine",
    "columns_of",
    "sort_reading_order",
    "BlockSpeaker",
    "ReadingPipeline",
    "ReadingSession",
    "TtsBlockSpeaker",
    "chunk_lines",
]
