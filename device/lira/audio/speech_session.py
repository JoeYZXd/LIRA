"""Bounded incremental text input for one logical output session."""
from __future__ import annotations
import asyncio
from collections import deque


class SpeechSession:
    max_pending_chars = 256

    def __init__(self, done):
        self.done = done
        self._pending = deque()
        self.pending_chars = 0
        self._ended = False
        self._changed = asyncio.Event()
        self._feeding = asyncio.Lock()

    async def feed(self, text: str) -> None:
        async with self._feeding:
            while text:
                if self._ended or self.done.is_set():
                    raise RuntimeError('speech session input is closed')
                room = self.max_pending_chars - self.pending_chars
                if not room:
                    self._changed.clear()
                    await self._changed.wait()
                    continue
                part, text = text[:room], text[room:]
                self._pending.append(part)
                self.pending_chars += len(part)
                self._changed.set()

    def finish_input(self) -> None:
        self._ended = True
        self._changed.set()

    def cancel(self) -> None:
        self._pending.clear()
        self.pending_chars = 0
        self.finish_input()

    async def fragments(self):
        while True:
            if self._pending:
                text = self._pending.popleft()
                self.pending_chars -= len(text)
                self._changed.set()
                yield text
            elif self._ended or self.done.is_set():
                return
            else:
                self._changed.clear()
                await self._changed.wait()
