"""分块阅读会话（U4）：缓存文本 + 位置游标 + 播放控制（R22/R21），拍摄→朗读编排（R4/R26）。

结构（计划 U4 Approach）：
  - `ReadingSession`：持有分块后的文本与游标，逐块经注入的 `BlockSpeaker` 播报；
    支持暂停/继续（立即停止当前块，恢复时重读未完成块）、再读一遍（R22：重复当前块，绝不重新拍摄）、
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
from dataclasses import dataclass
from lira.dialog.context import ReadingTaskFacts, ReadingTaskStatus
from typing import Awaitable, Callable, Protocol, Sequence

import cv2
import numpy as np

from lira.dialog.phrasebook import CAPTURE_GUIDANCE, OCR_COMPLETE
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

#: 音量步进与上下限（大声点/小声点，TTS volume 为播放满幅系数）。
#: 下限与 DeviceSettings.VOLUME_RANGE 对齐 0.1（ADV-06：0.2 下限时 0.1 起步
#: 的会话说「小声点」会被夹回 0.2 反而变响）。
_VOLUME_STEP = 0.2
_VOLUME_MIN = 0.1
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
    """TtsEngine 适配器：块播报经 TtsEngine.speak（合成+播放，完成事件等待）。

    volume：块播放音量（R21 朗读中"大声点/小声点"由 ReadingSession 直接改写）。
    speed：合成语速（M7 接线：装配层在会话边界从 DeviceSettings.tts_speed 同步）。
    """

    def __init__(self, tts: object, speed: float = 1.0) -> None:
        self._tts = tts
        self.volume: float = 1.0
        self.speed: float = speed

    def cancel(self, owner) -> None:
        self._tts.cancel_owner(owner)

    async def play(self, text: str, *, owner=None) -> None:
        if owner is None:
            raise ValueError("Reading playback requires an owner")
        done = self._tts.speak(text, volume=self.volume, speed=self.speed, owner=owner)
        try:
            await done.wait()
            if getattr(done, "status", "success") == "cancelled":
                raise asyncio.CancelledError
            if getattr(done, "status", "success") != "success":
                raise HalError("阅读播报失败")
        except asyncio.CancelledError:
            self._tts.cancel_owner(owner)
            await self._tts.stop(owner)
            raise


@dataclass(frozen=True)
class ReadingAudioOwner:
    task_id: object
    generation: int


class ReadingSession:
    """A retained reading task; only completed output advances its cursor."""
    def __init__(self, blocks, speaker, *, on_finished=None, task_id=None):
        self.blocks = list(blocks)
        self._speaker = speaker
        self._on_finished = on_finished
        self.task_id = task_id or object()
        self._cursor = 0
        self._last_completed = None
        self._active_block = None
        self._generation = 0
        self._audio_owner = None
        self._play_task = None
        self._resume_gate = asyncio.Event()
        self._resume_gate.set()
        self._stopped = False
        self._task = None
        self.failed = False

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    def _interrupt(self):
        self._generation += 1
        if self._audio_owner is not None and isinstance(self._speaker, TtsBlockSpeaker):
            self._speaker.cancel(self._audio_owner)
        if self._play_task is not None and not self._play_task.done():
            self._play_task.cancel()

    async def run(self):
        try:
            while self._cursor < len(self.blocks):
                await self._resume_gate.wait()
                if self._stopped:
                    return
                self._active_block = self._cursor
                generation = self._generation
                self._audio_owner = ReadingAudioOwner(self.task_id, generation)
                if isinstance(self._speaker, TtsBlockSpeaker):
                    play = self._speaker.play(self.blocks[self._cursor], owner=self._audio_owner)
                else:
                    play = self._speaker.play(self.blocks[self._cursor])
                self._play_task = asyncio.create_task(play)
                try:
                    await self._play_task
                except asyncio.CancelledError:
                    if self._stopped:
                        return
                    if generation != self._generation:
                        continue
                    raise
                if self._stopped:
                    return
                if generation != self._generation:
                    continue
                self._last_completed = self._cursor
                self._cursor += 1
                self._active_block = None
            if not self._stopped and self._on_finished is not None:
                self._on_finished()
        except asyncio.CancelledError:
            self._interrupt()
            raise
        except Exception:
            logging.exception("朗读会话播放失败")
            self.failed = True
            self._resume_gate.clear()
            if self._on_finished is not None:
                self._on_finished()

    def pause(self):
        self._resume_gate.clear()
        self._interrupt()

    def resume(self):
        if self._stopped:
            return
        self.failed = False
        self._resume_gate.set()
        self.start()

    def stop(self):
        self._stopped = True
        self._interrupt()
        self._resume_gate.set()

    def read_again(self):
        if self._stopped:
            return
        target = self._active_block if self._active_block is not None else self._last_completed
        if target is not None:
            self._cursor = target
        self._interrupt()

    def seek(self, block_index):
        self._cursor = max(0, min(block_index, len(self.blocks)))
        self._active_block = None
        self._interrupt()

    def volume_up(self):
        self._speaker.volume = min(self._speaker.volume + _VOLUME_STEP, _VOLUME_MAX)

    def volume_down(self):
        self._speaker.volume = max(self._speaker.volume - _VOLUME_STEP, _VOLUME_MIN)

    @property
    def cursor(self):
        return self._cursor

    @property
    def is_paused(self):
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
        self.raw_text: tuple[str, ...] = ()
        self._task_id = None
        self._cue_complete = False
        self._cue_failed = False
        self._cue_owner = None
        self._cue_generation = 0
        self._recovery_task = None
        #: 播放控制方法（main.py 直接绑到 DialogCallbacks 的六个占位）
        self.playback_pause = self._pause
        self.playback_resume = self._resume
        self.playback_stop = self._stop
        self.playback_read_again = self._repeat
        self.playback_volume_up = self._with_session(lambda s: s.volume_up())
        self.playback_volume_down = self._with_session(lambda s: s.volume_down())

    # ---------- 拍摄流程（R4/R26） ----------

    async def run_capture(self, *, read_original: bool = False, owner=None,
                          is_valid: Callable[[], bool] = lambda: True,
                          on_capture_done=None, on_reading_finished=None) -> None:
        """R4 引导 → 拍摄 → OCR；空结果/失败 → on_capture_done(False)（R26 信号）。

        可取消（F1）：状态机取消离开 CAPTURING 时经任务句柄 cancel 本协程——
        CancelledError 在任一 await 点即中止流程，不回执 on_capture_done、
        不启动正文。已完成识别的缓存保持暂停，只有新的明确继续指令才能恢复；
        停止或被新阅读取代的任务不会重新发布缓存。
        """
        notify = on_capture_done or self._on_capture_done
        created_session = None
        capture_id = object()
        self._stop()
        self._task_id = capture_id
        def check():
            if not is_valid() or self._task_id is not capture_id:
                raise asyncio.CancelledError
        async def play(text):
            check()
            if isinstance(self._speaker, TtsBlockSpeaker):
                await self._speaker.play(text, owner=owner)
            else:
                await self._speaker.play(text)
            check()
        try:
            check()
            await play(CAPTURE_GUIDANCE)
            image = await self._grab()
            check()
            if image is None:
                notify(False)
                return
            try:
                lines = await asyncio.to_thread(self._engine.read, image)
            except Exception:  # noqa: BLE001 - 推理故障按"未检出"走 R26 引导
                logging.exception("OCR 推理失败")
                lines = []
            check()
            if not lines:
                logging.info("OCR 未检出文本行（R26 引导信号）")
                notify(False)
                return
            raw_text = [line.text for line in lines if line.text.strip()]
            if not raw_text:
                notify(False)
                return
            # Publish cached text only while capture still owns this task.
            text = raw_text if read_original else await self._polish(raw_text)
            check()
            self.raw_text = tuple(raw_text)
            blocks = chunk_lines(text, self._block_chars)
            def finished():
                if self._session is created_session and self._task_id is capture_id:
                    callback = on_reading_finished or self._on_reading_finished
                    if callback is not None:
                        callback()
            created_session = ReadingSession(blocks, self._speaker, on_finished=finished,
                                             task_id=capture_id)
            created_session.pause()
            self._session = created_session
            await play(OCR_COMPLETE)
            self._cue_complete = True
            check()
            notify(True)
            created_session.resume()
        except asyncio.CancelledError:
            if created_session is not None and self._session is created_session:
                # Completed OCR is an independent retained task. Interrupted
                # feedback cannot emit late output, but explicit continue may
                # retry its cue using this cache after the chat owner changes.
                created_session.pause()
            raise
        except Exception:  # noqa: BLE001 - 拍摄/引导非预期故障也必须回执（评审：CAPTURING 卡死防线）
            # F3 同款纪律：状态机 CAPTURING 无计时器（拍摄开始即 disarm），协程若
            # 带 unretrieved 异常死掉，on_capture_done 永不到达 -> 引擎永久卡在
            # CAPTURING（唤醒词失效）。终局故障按"未检出"走 R26 引导/重拍。
            logging.exception("拍摄流程非预期故障（按未检出处理）")
            if created_session is not None and self._session is created_session:
                self._cue_failed = True
            if is_valid():
                notify(False)

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

    def _cancel_cue(self):
        owner, self._cue_owner = self._cue_owner, None
        task, self._recovery_task = self._recovery_task, None
        if owner is not None and isinstance(self._speaker, TtsBlockSpeaker):
            self._speaker.cancel(owner)
        if task is not None and not task.done():
            task.cancel()

    def _pause(self):
        if self._session is not None:
            self._session.pause()
        self._cancel_cue()

    def _stop(self):
        if self._session is not None:
            self._session.stop()
        self._cancel_cue()
        self._session = None
        self.raw_text = ()
        self._task_id = None
        self._cue_complete = False
        self._cue_failed = False

    def _resume(self):
        session = self._session
        if session is None:
            return
        if self._cue_complete:
            session.resume()
        elif self._recovery_task is None or self._recovery_task.done():
            self._cue_generation += 1
            owner = self._cue_owner = ReadingAudioOwner((session.task_id, "completion_cue"), self._cue_generation)
            async def recover():
                try:
                    if self._session is not session or self._cue_owner != owner:
                        return
                    if isinstance(self._speaker, TtsBlockSpeaker):
                        await self._speaker.play(OCR_COMPLETE, owner=owner)
                    else:
                        await self._speaker.play(OCR_COMPLETE)
                    if self._session is session and self._cue_owner == owner:
                        self._cue_complete = True
                        self._cue_failed = False
                        session.resume()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._session is session and self._cue_owner == owner:
                        self._cue_failed = True
                    logging.exception("阅读完成提示重试失败")
                finally:
                    if self._cue_owner == owner:
                        self._cue_owner = None
            self._recovery_task = asyncio.create_task(recover())

    def _repeat(self):
        if self._session is not None:
            self._session.read_again()
            self._resume()

    def task_facts(self):
        session = self._session
        if session is None:
            return None
        status = (ReadingTaskStatus.FAILED if session.failed or self._cue_failed
                  else ReadingTaskStatus.COMPLETED if session.cursor >= len(session.blocks)
                  else ReadingTaskStatus.PAUSED if session.is_paused else ReadingTaskStatus.READING)
        return ReadingTaskFacts(status, resumable=bool(session.blocks))

    @property
    def session(self) -> ReadingSession | None:
        """当前活动会话（测试/装配观测用）。"""
        return self._session
