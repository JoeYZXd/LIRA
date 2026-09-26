"""U4 阅读会话与拍摄管线测试（计划 Test scenarios 4 条 + R22/R26/分块）。

策略（计划执行要求 3）：
  - 会话/管线用 fake engine + FakeCamera + RecordingSpeaker 注入，确定性断言；
  - 真实 OCR（PP-OCRv4 onnx + PIL 渲染中文样张）为集成测试，模型或字体缺失时 skip。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from lira.hal import Camera, HalError
from lira.vision.engine import OcrEngine, OcrLine, crop_rotated_box
from lira.vision.ocr_x86 import DET_ONNX, REC_DICT, REC_ONNX, OnnxOcrEngine
from lira.vision.reading import (
    CAPTURE_GUIDANCE,
    ReadingPipeline,
    ReadingSession,
    chunk_lines,
)

MODELS_READY = DET_ONNX.is_file() and REC_ONNX.is_file() and REC_DICT.is_file()

_CJK_FONT_CANDIDATES = (
    Path("/usr/share/fonts/windows/simhei.ttf"),
    Path("/usr/share/fonts/windows/Deng.ttf"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
)
CJK_FONT = next((p for p in _CJK_FONT_CANDIDATES if p.is_file()), None)


# ---------- 测试替身 ----------


class RecordingSpeaker:
    """块播放录制器：记录播报顺序/音量，可在播报中触发会话控制（驱动暂停等）。"""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.volume: float = 1.0
        #: play(text) 时触发的 async 钩子（模拟语音命令到达）
        self.on_play = None

    async def play(self, text: str) -> None:
        self.played.append(text)
        if self.on_play is not None:
            await self.on_play(text)


class FakeEngine(OcrEngine):
    """固定结果的 OCR 替身：read() 返回预置 OcrLine 列表（空列表=未检出）。"""

    def __init__(self, results: list[OcrLine]) -> None:
        self.results = results
        self.read_calls = 0

    def detect(self, image: np.ndarray) -> list[np.ndarray]:  # pragma: no cover
        return []

    def recognize(self, crop: np.ndarray) -> str:  # pragma: no cover
        return ""

    def read(self, image: np.ndarray) -> list[OcrLine]:
        self.read_calls += 1
        return list(self.results)


class FakeCamera(Camera):
    """拍摄替身：返回预置字节或抛 HalError；计数断言用（R22 不重拍）。"""

    def __init__(self, payload: bytes | Exception) -> None:
        self._payload = payload
        self.capture_count = 0

    async def capture(self) -> bytes:
        self.capture_count += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def line(x: float, y: float, text: str) -> OcrLine:
    poly = ((x, y), (x + 200, y), (x + 200, y + 40), (x, y + 40))
    return OcrLine(polygon=poly, text=text)


def tiny_png() -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", np.full((8, 8, 3), 255, dtype=np.uint8))
    assert ok
    return buf.tobytes()


async def settle(times: int = 5) -> None:
    """让出事件循环若干拍，使后台朗读任务推进到阻塞点。"""
    for _ in range(times):
        await asyncio.sleep(0)


# ---------- chunk_lines（R22 分块） ----------


class TestChunkLines:
    def test_joins_short_lines_up_to_budget(self):
        blocks = chunk_lines(["第一行", "第二行", "第三行"], max_chars=7)
        assert blocks == ["第一行第二行", "第三行"]

    def test_splits_overlong_line(self):
        blocks = chunk_lines(["字" * 25], max_chars=10)
        assert blocks == ["字" * 10, "字" * 10, "字" * 5]

    def test_empty_and_blank_lines_dropped(self):
        assert chunk_lines(["", "  ", "内容"]) == ["内容"]
        assert chunk_lines([]) == []


# ---------- ReadingSession：顺序 / 暂停继续 / 再读一遍 / 停止（R22） ----------


class TestReadingSession:
    async def test_plays_blocks_in_order_then_finishes(self):
        played_done = asyncio.Event()
        speaker = RecordingSpeaker()
        session = ReadingSession(["甲", "乙", "丙"], speaker, on_finished=played_done.set)
        session.start()
        await asyncio.wait_for(played_done.wait(), 5)
        assert speaker.played == ["甲", "乙", "丙"]
        assert session.cursor == 3

    async def test_pause_before_start_blocks_playback_then_resume(self):
        speaker = RecordingSpeaker()
        session = ReadingSession(["甲", "乙"], speaker)
        session.pause()
        assert session.is_paused
        session.start()
        await settle()
        assert speaker.played == [], "暂停态不得开始播报"

        finished = asyncio.Event()
        session._on_finished = finished.set
        session.resume()
        await asyncio.wait_for(finished.wait(), 5)
        assert speaker.played == ["甲", "乙"]

    async def test_pause_during_block_halts_at_boundary_then_resumes(self):
        """Test scenario 4（前半）: 朗读中"暂停"→ 当前块播完停住；"继续"→ 接着读。"""
        speaker = RecordingSpeaker()
        session = ReadingSession(["第一块", "第二块", "第三块"], speaker)
        finished = asyncio.Event()
        session._on_finished = finished.set

        async def hook(text: str) -> None:
            if text == "第二块":
                speaker.on_play = None
                session.pause()

        speaker.on_play = hook
        session.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if session.is_paused:
                break
        assert session.is_paused
        await settle(20)  # 让当前块播完、run 停在 gate 上
        assert speaker.played == ["第一块", "第二块"], "暂停应停在块边界"

        session.resume()
        await asyncio.wait_for(finished.wait(), 5)
        assert speaker.played == ["第一块", "第二块", "第三块"], "恢复后不得跳块或重播"

    async def test_read_again_repeats_current_block_without_advancing(self):
        """R22: "再读一遍" 重复当前块；游标不前进。"""
        speaker = RecordingSpeaker()
        session = ReadingSession(["甲", "乙"], speaker)
        finished = asyncio.Event()
        session._on_finished = finished.set

        async def hook(text: str) -> None:
            if text == "甲" and not session.is_paused:
                # 只在第一次播甲时触发一次
                speaker.on_play = None
                session.read_again()

        speaker.on_play = hook
        session.start()
        await asyncio.wait_for(finished.wait(), 5)
        assert speaker.played == ["甲", "甲", "乙"]
        assert session.cursor == 2

    async def test_stop_ends_session_without_finish_callback(self):
        speaker = RecordingSpeaker()
        session = ReadingSession(["甲", "乙", "丙"], speaker)
        finished = asyncio.Event()
        session._on_finished = finished.set

        async def hook(text: str) -> None:
            if text == "乙":
                speaker.on_play = None
                session.stop()
                await settle()

        speaker.on_play = hook
        session.start()
        await asyncio.wait_for(session._task, 5)
        assert speaker.played == ["甲", "乙"], "停止后不得继续读后续块"
        assert not finished.is_set(), "停止不触发自然结束回调（状态机自行回待机）"

    async def test_seek_jumps_to_block_boundary(self):
        speaker = RecordingSpeaker()
        session = ReadingSession(["甲", "乙", "丙"], speaker)
        session.seek(2)
        assert session.cursor == 2
        finished = asyncio.Event()
        session._on_finished = finished.set
        session.start()
        await asyncio.wait_for(finished.wait(), 5)
        assert speaker.played == ["丙"]

    async def test_volume_steps_clamped(self):
        speaker = RecordingSpeaker()
        session = ReadingSession(["甲"], speaker)
        for _ in range(10):
            session.volume_up()
        assert speaker.volume == pytest.approx(2.0)
        for _ in range(10):
            session.volume_down()
        assert speaker.volume == pytest.approx(0.2)


# ---------- ReadingPipeline：R4 引导 / OCR 空结果 R26 信号 / R22 不重拍 ----------


class TestReadingPipeline:
    def _build(self, engine: OcrEngine, camera_payload: bytes | Exception):
        camera = FakeCamera(camera_payload)
        speaker = RecordingSpeaker()
        captured: dict[str, object] = {}
        pipeline = ReadingPipeline(
            camera,
            engine,
            speaker,
            on_capture_done=lambda ok: captured.setdefault("done", []).append(ok),
            on_reading_finished=lambda: captured.setdefault("finished", True),
        )
        return camera, speaker, captured, pipeline

    async def test_happy_path_speaks_guidance_then_blocks(self):
        engine = FakeEngine([line(0, 0, "第一段"), line(0, 50, "第二段")])
        camera, speaker, captured, pipeline = self._build(engine, tiny_png())
        await asyncio.wait_for(pipeline.run_capture(), 5)

        assert captured["done"] == [True]
        assert speaker.played[0] == CAPTURE_GUIDANCE, "R4 拍摄语音引导"
        await settle(20)  # 等朗读任务推进
        assert speaker.played[1:] == chunk_lines(["第一段", "第二段"])
        assert camera.capture_count == 1
        await settle(20)
        assert captured.get("finished"), "自然读完应触发 on_reading_finished（状态机回待机）"

    async def test_empty_ocr_signals_failure(self):
        """Error path: 空白/糊图 → engine 空结果 → 未检出信号（R26 引导在状态机）。"""
        engine = FakeEngine([])
        camera, speaker, captured, pipeline = self._build(engine, tiny_png())
        await asyncio.wait_for(pipeline.run_capture(), 5)
        assert captured["done"] == [False]
        assert speaker.played == [CAPTURE_GUIDANCE], "未检出不得开始朗读"

    async def test_camera_error_signals_failure(self):
        engine = FakeEngine([])
        camera, speaker, captured, pipeline = self._build(engine, HalError("摄像头故障"))
        await asyncio.wait_for(pipeline.run_capture(), 5)
        assert captured["done"] == [False]

    async def test_read_again_does_not_recapture(self):
        """Test scenario 4（后半）: 朗读中"再读一遍"不得触发 camera 调用（mock 计数断言）。"""
        engine = FakeEngine([line(0, 0, "甲"), line(0, 50, "乙")])
        camera, speaker, captured, pipeline = self._build(engine, tiny_png())

        async def hook(text: str) -> None:
            if text == "甲" and pipeline.session is not None:
                speaker.on_play = None
                pipeline.playback_read_again()

        speaker.on_play = hook
        await asyncio.wait_for(pipeline.run_capture(), 5)
        await settle(20)
        assert captured.get("finished")
        assert camera.capture_count == 1, "R22：再读一遍不得重新拍摄"

    def test_playback_controls_noop_without_session(self):
        engine = FakeEngine([])
        _, _, _, pipeline = self._build(engine, tiny_png())
        for control in (
            pipeline.playback_pause,
            pipeline.playback_resume,
            pipeline.playback_stop,
            pipeline.playback_read_again,
            pipeline.playback_volume_up,
            pipeline.playback_volume_down,
        ):
            control()  # 不应抛异常


# ---------- 真实 OCR 集成（模型+字体就绪时；Verification 的样张等价物） ----------


def _render_text_image(lines: list[str], cols: list[list[str]] | None = None):
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(CJK_FONT), 40)
    if cols is None:
        img = Image.new("RGB", (900, 200 + 70 * len(lines)), "white")
        draw = ImageDraw.Draw(img)
        for i, text in enumerate(lines):
            draw.text((50, 50 + i * 70), text, fill="black", font=font)
    else:
        img = Image.new("RGB", (1200, 500), "white")
        draw = ImageDraw.Draw(img)
        for cx, texts in zip((60, 460, 860), cols):
            for i, text in enumerate(texts):
                draw.text((cx, 60 + i * 90), text, fill="black", font=font)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


@pytest.fixture(scope="module")
def ocr_engine() -> OnnxOcrEngine:
    return OnnxOcrEngine()


@pytest.mark.skipif(not MODELS_READY, reason="OCR 模型未就绪（models/ppocrv4_*.onnx）")
@pytest.mark.skipif(CJK_FONT is None, reason="未找到中文字体，无法渲染样张")
class TestRealOcr:
    def test_single_column_letter_order(self, ocr_engine: OnnxOcrEngine):
        """Happy path: 单栏信件样张 → 识别文本顺序正确。"""
        expected = ["今天天气很好", "我们一起去公园散步", "祝您身体健康"]
        image = _render_text_image(expected)
        lines = ocr_engine.read(image)
        texts = [line.text for line in lines]
        assert len(texts) == len(expected)
        for got, want in zip(texts, expected):
            assert want in got, f"应识别出 {want!r}，实际 {got!r}"

    def test_three_columns_grouped_by_column(self, ocr_engine: OnnxOcrEngine):
        """Edge case: 三栏样张 → 阅读顺序按列分组（左栏读完读中栏、右栏）。"""
        cols = [
            ["左栏第一行", "左栏第二行", "左栏第三行"],
            ["中栏第一行", "中栏第二行", "中栏第三行"],
            ["右栏第一行", "右栏第二行", "右栏第三行"],
        ]
        image = _render_text_image([], cols=cols)
        texts = [line.text for line in ocr_engine.read(image)]
        flat = "".join(texts)
        # 全部左栏行出现在任何中栏行之前，中栏读完才读右栏
        assert flat.index("左栏第三行") < flat.index("中栏第一行")
        assert flat.index("中栏第三行") < flat.index("右栏第一行")
        for group in cols:
            positions = [flat.index(group[0]), flat.index(group[1]), flat.index(group[2])]
            assert positions == sorted(positions), f"{group} 栏内应自上而下"

    def test_blank_image_yields_empty_result(self, ocr_engine: OnnxOcrEngine):
        """Error path: 空白图 → 空结果 → 上层发 R26 引导信号。"""
        image = np.full((480, 640, 3), 255, dtype=np.uint8)
        assert ocr_engine.read(image) == []

    def test_crop_rotated_box_shapes(self, ocr_engine: OnnxOcrEngine):
        image = np.full((100, 300, 3), 255, dtype=np.uint8)
        poly = np.array([[10, 10], [290, 10], [290, 50], [10, 50]], dtype=np.float32)
        crop = crop_rotated_box(image, poly)
        assert crop.shape[0] == 40 and crop.shape[1] == 280
