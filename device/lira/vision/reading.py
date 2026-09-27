"""分块阅读会话（U4）：缓存文本 + 位置游标 + 播放控制（R22/R21），拍摄→朗读编排（R4/R26）。

结构（计划 U4 Approach）：
  - `ReadingSession`：持有分块后的文本与游标，逐块经注入的 `BlockSpeaker` 播报；
    支持暂停/继续（块边界生效）、再读一遍（R22：重复当前块，绝不重新拍摄）、
    停止、seek 到块边界、音量增减。自然读完触发 `on_finished`（状态机据此回待机）；
    "停止"由状态机直接回待机，不触发 `on_finished`。
  - `ReadingPipeline`：R4 拍摄语音引导 → 拍摄 → OCR → 版面排序 → 分块 → 开播；
    空结果/拍摄失败 → `on_capture_done(False)` 信号（R26 引导/重拍由状态机 U3 逻辑驱动，
    本模块只发信号，不自行计数——单一职责，重试次数归状态机 MAX_CAPTURE_RETRIES）。
  - 播放控制入口为六个无副作用方法（与 U3 `DialogCallbacks` 播放占位同名同义），
    main.py 装配时把它们接到状态机回调即可；无活动会话时全部安全 no-op。

隐私纪律：朗读内容不落日志（计划「日志纪律」）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol, Sequence

import cv2
import numpy as np

from lira.dialog.phrasebook import CAPTURE_GUIDANCE
from lira.hal import Camera, HalError
from lira.vision.engine import OcrEngine

__all__ = [
    "BlockSpeaker",
    "ReadingSession",
    "ReadingPipeline",
    "TtsBlockSpeaker",
    "chunk_lines",
]

#: 单块朗读字符预算（块间停顿是白名单命令的接受窗口，块不宜过长；R22）
DEFAULT_BLOCK_CHARS = 60

#: 音量步进与上下限（大声点/小声点，TTS volume 为播放满幅系数）
_VOLUME_STEP = 0.2
_VOLUME_MIN = 0.2
_VOLUME_MAX = 2.0


def chunk_lines(lines: Sequence[str], max_chars: int = DEFAULT_BLOCK_CHARS) -> list[str]:
    """把有序文本行合并/切分为朗读块（每块 ≤ max_chars，块边界落在行/句边界）。"""
    blocks: list[str] = []
    buffer = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        while len(line) > max_chars:
            if buffer:
                blocks.append(buffer)
                buffer = ""
            blocks.append(line[:max_chars])
            line = line[max_chars:]
        if buffer and len(buffer) + len(line) > max_chars:
            blocks.append(buffer)
            buffer = line
        else:
            buffer = buffer + line if buffer else line
    if buffer:
        blocks.append(buffer)
    return blocks


class BlockSpeaker(Protocol):
    """朗读块播放器协议：真实实现经 TtsEngine；测试用录制器注入。"""

    volume: float

    async def play(self, text: str) -> None:
        """播报一块文本，返回即代表本块播放完成。"""


class TtsBlockSpeaker:
    """TtsEngine 适配器：块播报经 TtsEngine.speak（合成+播放，完成事件等待）。"""

    def __init__(self, tts: object) -> None:  # TtsEngine（避免循环依赖用鸭子类型）
        self._tts = tts
        self.volume: float = 1.0

    async def play(self, text: str) -> None:
        done = self._tts.speak(text, volume=self.volume)  # type: ignore[attr-defined]
        await done.wait()


class ReadingSession:
    """单次阅读会话：块序列 + 游标 + 播放控制（R22）。全部方法可在控制回调中同步调用。"""

    def __init__(
        self,
        blocks: Sequence[str],
        speaker: BlockSpeaker,
        *,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        self.blocks = list(blocks)
        self._speaker = speaker
        self._on_finished = on_finished
        self._cursor = 0
        self._resume_gate = asyncio.Event()
        self._resume_gate.set()
        self._replay_requested = False
        self._stopped = False
        self._task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def run(self) -> None:
        """逐块播报直至读完（自然结束触发 on_finished）或被 stop()。"""
        try:
            while self._cursor < len(self.blocks):
                await self._resume_gate.wait()
                if self._stopped:
                    return
                await self._speaker.play(self.blocks[self._cursor])
                if self._stopped:
                    return
                if self._replay_requested:
                    # R22：再读一遍——重复当前块，游标不动、不重新拍摄
                    self._replay_requested = False
                    continue
                self._cursor += 1
            if self._on_finished is not None:
                self._on_finished()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 播放故障不得拖垮主循环
            logging.exception("朗读会话异常终止")
            # F3：终局播放故障也必须触发 on_finished——否则状态机收不到
            # 回执，永久卡在 READING。游标不动（故障块不前进）。
            if self._on_finished is not None:
                self._on_finished()

    # ---------- 播放控制（R21/R22 白名单命令的落点） ----------

    def pause(self) -> None:
        """暂停：当前块播完后停住（块粒度，x86 TTS 全块播报的语义边界）。"""
        self._resume_gate.clear()

    def resume(self) -> None:
        """继续：从暂停处接着读（暂停时当前块已播完，继续读下一块）。"""
        self._resume_gate.set()

    def stop(self) -> None:
        """停止：终止会话（不触发 on_finished，状态机自行回待机）。"""
        self._stopped = True
        self._resume_gate.set()  # 唤醒可能停在 gate 上的 run

    def read_again(self) -> None:
        """R22：重复当前块（若恰在块间，则把即将播放的块播两遍）。

        F8：暂停态（gate 未放行）时游标已停在"刚听完那块"的下一块上——
        此时"再读一遍"语义是重听刚播完的那块，把游标退回一块即可（恢复后
        自然重播），不再置 replay 标记（否则会错误重复下一块）。
        """
        if self.is_paused and self._cursor > 0:
            self._cursor -= 1
            return
        self._replay_requested = True

    def seek(self, block_index: int) -> None:
        """seek 到块边界（0 ≤ index ≤ len）。"""
        self._cursor = max(0, min(block_index, len(self.blocks)))

    def volume_up(self) -> None:
        self._speaker.volume = min(self._speaker.volume + _VOLUME_STEP, _VOLUME_MAX)

    def volume_down(self) -> None:
        self._speaker.volume = max(self._speaker.volume - _VOLUME_STEP, _VOLUME_MIN)

    # ---------- 观测（测试/装配图用） ----------

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def is_paused(self) -> bool:
        return not self._resume_gate.is_set()


class ReadingPipeline:
    """拍摄→OCR→排序→分块→开播的编排器 + READING 态播放控制入口。

    Args:
        camera: HAL Camera（capture() 返回 JPEG 字节）。
        engine: OcrEngine（detect+recognize，或注入 fake）。
        speaker: 块播放器（拍摄引导与朗读共用）。
        on_capture_done: 状态机回执（success 信号，R26 重试计数在状态机）。
        on_reading_finished: 自然读完回执（状态机回待机，R22）。
    """

    def __init__(
        self,
        camera: Camera,
        engine: OcrEngine,
        speaker: BlockSpeaker,
        *,
        on_capture_done: Callable[[bool], None],
        on_reading_finished: Callable[[], None] | None = None,
        block_chars: int = DEFAULT_BLOCK_CHARS,
        text_polisher: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        """Args 补充（AE2 装配钩子，不改变 OCR 语义）：

        text_polisher: 可选的文本口语化加工（注入 LlmClient 包装），在 OCR
        之后、分块之前应用。异常或空结果一律回退原文——"不静默、不拒读"。
        """
        self._camera = camera
        self._engine = engine
        self._speaker = speaker
        self._on_capture_done = on_capture_done
        self._on_reading_finished = on_reading_finished
        self._block_chars = block_chars
        self._text_polisher = text_polisher
        self._session: ReadingSession | None = None
        #: 播放控制方法（main.py 直接绑到 DialogCallbacks 的六个占位）
        self.playback_pause = self._with_session(lambda s: s.pause())
        self.playback_resume = self._with_session(lambda s: s.resume())
        self.playback_stop = self._with_session(lambda s: s.stop())
        self.playback_read_again = self._with_session(lambda s: s.read_again())
        self.playback_volume_up = self._with_session(lambda s: s.volume_up())
        self.playback_volume_down = self._with_session(lambda s: s.volume_down())

    # ---------- 拍摄流程（R4/R26） ----------

    async def run_capture(self) -> None:
        """R4 引导 → 拍摄 → OCR；空结果/失败 → on_capture_done(False)（R26 信号）。

        可取消（F1）：状态机取消离开 CAPTURING 时经任务句柄 cancel 本协程——
        CancelledError 在任一 await 点即中止流程，不回执 on_capture_done、
        不创建朗读会话（防御性兜底：万一取消晚到，也先停掉已开的会话再上抛）。
        """
        try:
            await self._speaker.play(CAPTURE_GUIDANCE)
            image = await self._grab()
            if image is None:
                self._on_capture_done(False)
                return
            try:
                lines = await asyncio.to_thread(self._engine.read, image)
            except Exception:  # noqa: BLE001 - 推理故障按"未检出"走 R26 引导
                logging.exception("OCR 推理失败")
                lines = []
            if not lines:
                logging.info("OCR 未检出文本行（R26 引导信号）")
                self._on_capture_done(False)
                return
            raw_text = [line.text for line in lines]
            blocks = chunk_lines(await self._polish(raw_text), self._block_chars)
            self._session = ReadingSession(
                blocks,
                self._speaker,
                on_finished=self._handle_finished,
            )
            self._session.start()
            self._on_capture_done(True)
        except asyncio.CancelledError:
            if self._session is not None:
                self._session.stop()
                self._session = None
            raise

    async def _polish(self, raw_lines: list[str]) -> list[str]:
        """可选口语化加工（AE2）：失败/空结果回退原文，不静默、不拒读。"""
        if self._text_polisher is None:
            return raw_lines
        try:
            polished = await self._text_polisher("\n".join(raw_lines))
        except Exception:  # noqa: BLE001 - 加工失败按原文朗读
            logging.exception("文本口语化失败（按原文朗读）")
            return raw_lines
        if not polished:
            return raw_lines
        polished_lines = [ln.strip() for ln in polished.splitlines() if ln.strip()]
        return polished_lines or raw_lines

    async def _grab(self) -> np.ndarray | None:
        try:
            raw = await self._camera.capture()
        except HalError:
            logging.warning("拍摄失败（R26 引导信号）")
            return None
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            logging.warning("拍摄内容无法解码（R26 引导信号）")
            return None
        return image

    # ---------- 播放控制 ----------

    def _with_session(self, action: Callable[[ReadingSession], None]) -> Callable[[], None]:
        def call() -> None:
            if self._session is not None:
                action(self._session)

        return call

    def _handle_finished(self) -> None:
        self._session = None
        if self._on_reading_finished is not None:
            self._on_reading_finished()

    @property
    def session(self) -> ReadingSession | None:
        """当前活动会话（测试/装配观测用）。"""
        return self._session
