"""Deterministic interruptions verify cursor movement follows completed output."""
import asyncio

from lira.vision.reading import ReadingSession


class BlockingSpeaker:
    volume = 1.0

    def __init__(self):
        self.started = asyncio.Queue()
        self.cancelled = asyncio.Queue()

    async def play(self, text):
        gate = asyncio.Event()
        await self.started.put((text, gate))
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await self.cancelled.put(text)
            raise


async def test_pause_interrupts_output_and_resume_restarts_unfinished_block():
    speaker = BlockingSpeaker()
    session = ReadingSession(["第一块", "第二块"], speaker)
    session.start()
    try:
        text, _ = await asyncio.wait_for(speaker.started.get(), 0.5)
        assert text == "第一块"
        session.pause()
        assert await asyncio.wait_for(speaker.cancelled.get(), 0.2) == "第一块"
        assert session.cursor == 0
        session.resume()
        text, gate = await asyncio.wait_for(speaker.started.get(), 0.5)
        assert text == "第一块"
        gate.set()
        text, gate = await asyncio.wait_for(speaker.started.get(), 0.5)
        assert text == "第二块"
        gate.set()
        await asyncio.wait_for(session._task, 0.5)
        assert session.cursor == 2
    finally:
        session.stop()
        session._task.cancel()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_stop_interrupts_without_reporting_natural_completion():
    speaker = BlockingSpeaker()
    finished = []
    session = ReadingSession(["第一块"], speaker, on_finished=lambda: finished.append(True))
    session.start()
    try:
        await asyncio.wait_for(speaker.started.get(), 0.5)
        session.stop()
        assert await asyncio.wait_for(speaker.cancelled.get(), 0.2) == "第一块"
        await asyncio.wait_for(session._task, 0.5)
        assert finished == [] and session.cursor == 0
    finally:
        session._task.cancel()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_repeat_after_interruption_keeps_active_block():
    speaker = BlockingSpeaker()
    session = ReadingSession(["第一块", "第二块"], speaker)
    session.start()
    try:
        _, gate = await asyncio.wait_for(speaker.started.get(), 0.5)
        gate.set()
        text, _ = await asyncio.wait_for(speaker.started.get(), 0.5)
        assert text == "第二块"
        session.pause()
        assert await asyncio.wait_for(speaker.cancelled.get(), 0.2) == "第二块"
        session.read_again()
        session.resume()
        text, gate = await asyncio.wait_for(speaker.started.get(), 0.5)
        assert text == "第二块"
        gate.set()
        await asyncio.wait_for(session._task, 0.5)
    finally:
        session.stop()
        session._task.cancel()
        await asyncio.gather(session._task, return_exceptions=True)


async def test_tts_adapter_cancellation_awaits_owned_physical_stop():
    from lira.vision.reading import TtsBlockSpeaker, ReadingAudioOwner
    class Tts:
        def __init__(self):
            self.done = asyncio.Event()
            self.stopping = asyncio.Event()
            self.stopped = asyncio.Event()
            self.cancelled = []
            self.owners = []
        def speak(self, text, **kwargs):
            self.owners.append(kwargs["owner"])
            return self.done
        def cancel_owner(self, owner):
            self.cancelled.append(owner)
        async def stop(self, owner):
            assert owner is self.owners[0]
            self.stopping.set()
            await self.stopped.wait()
    tts = Tts()
    owner = ReadingAudioOwner(object(), 1)
    task = asyncio.create_task(TtsBlockSpeaker(tts).play("缓存原文", owner=owner))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait_for(tts.stopping.wait(), 0.5)
    assert not task.done()
    assert tts.cancelled == [owner] and tts.owners == [owner]
    tts.stopped.set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
