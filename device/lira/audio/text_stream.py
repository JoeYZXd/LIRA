"""Bounded, pull-driven final-answer text delivery shared by both LLM paths.

The callback may be synchronous or awaitable. Awaiting it is the business
backpressure boundary; observers must remain outside this callback.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time

from lira.audio.speech_text import spoken_text


def _boundaries(text: str):
    """Yield safe offsets without breaking links, numbers or English words.

    A trailing period is ambiguous until the following character arrives.
    Unclosed markup is retained, then rejected at the hard capacity limit.
    """
    bracket = paren = 0
    url = False
    tick = False
    emphasis = None
    for i, char in enumerate(text):
        if text.startswith(('http://', 'https://'), i):
            url = True
        if url and char.isspace():
            url = False
        if char == '`':
            tick = not tick
        if char in '*_' and (i == 0 or not text[i - 1].isalnum() or emphasis):
            if emphasis == char:
                emphasis = None
            elif emphasis is None:
                emphasis = char
        if char == '[':
            bracket += 1
        elif char == ']':
            bracket = max(0, bracket - 1)
            # Keep the label until it can be distinguished from a link.
            if i + 1 == len(text):
                continue
        elif char == '(' and i and text[i - 1] == ']':
            paren += 1
        elif char == ')' and paren:
            paren -= 1
        if bracket or paren or url or tick or emphasis:
            continue
        if char == ']' and i + 1 < len(text) and text[i + 1] == '(':
            continue
        sentence = char in '。！？!?；;\n' or (
            char == '.' and i + 1 < len(text) and text[i + 1].isspace())
        clause = char.isspace() or char in '，,、' or '\u3400' <= char <= '\u9fff'
        if sentence or clause:
            yield i + 1, sentence


async def consume_text(source, on_segment=None, *, flush_seconds: float = .25,
                       max_buffer_chars: int = 2048, min_chars: int = 12,
                       target_chars: int = 48, check_active=lambda: None,
                       incremental: bool = False) -> str:
    """Consume an async iterator of raw content deltas and return exact raw text.

    Only one pending read is held while waiting for a timeout. No producer task
    or unbounded speech queue exists here. Unsafe overlong tokens fail rather
    than being split or silently dropped. ``incremental`` lowers the safe-prefix
    threshold for a future bidirectional backend; incomplete markup still waits.
    """
    if flush_seconds <= 0 or max_buffer_chars < 1:
        raise ValueError('invalid text stream limits')
    parts = []  # Full reply is retained separately for UI/history, never queued.
    buffer = ''
    pending = None
    iterator = source.__aiter__()
    deadline = time.monotonic() + flush_seconds

    async def emit(count):
        nonlocal buffer, deadline
        check_active()
        raw, buffer = buffer[:count], buffer[count:]
        # A truncated final emphasis delimiter is presentation, not speech.
        cleaned = spoken_text(raw).strip('*_`')
        if cleaned and raw[-1:].isspace():
            cleaned += ' '
        if cleaned and raw[:1].isspace():
            cleaned = ' ' + cleaned
        if cleaned and on_segment is not None:
            result = on_segment(cleaned)
            if inspect.isawaitable(result):
                await result
        deadline = time.monotonic() + flush_seconds

    async def drain(timed=False):
        while buffer:
            candidates = list(_boundaries(buffer))
            boundary = next((n for n, sentence in candidates
                             if sentence and min_chars <= n <= target_chars), None)
            if boundary is None and (len(buffer) >= target_chars or timed or incremental):
                eligible = [n for n, _ in candidates if n <= target_chars]
                boundary = max(eligible, default=None)
            if boundary is None:
                break
            await emit(boundary)

    try:
        while True:
            check_active()
            if pending is None:
                pending = asyncio.ensure_future(anext(iterator))
            timeout = max(0, deadline - time.monotonic()) if buffer else flush_seconds
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if not done:
                check_active()
                await drain(timed=True)
                deadline = time.monotonic() + flush_seconds
                continue
            try:
                delta = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            check_active()
            if not delta:
                continue
            parts.append(delta)
            if on_segment is None:
                continue
            # Bound the staging buffer even if an SDK returns one huge delta.
            while delta:
                room = max_buffer_chars - len(buffer)
                if room == 0:
                    raise ValueError('text stream buffer limit exceeded')
                buffer += delta[:room]
                delta = delta[room:]
                await drain()
                if len(buffer) >= max_buffer_chars:
                    await drain(timed=True)
                    if len(buffer) >= max_buffer_chars:
                        raise ValueError('text stream buffer limit exceeded')
        if buffer:
            # At EOF incomplete links/URLs are removed rather than spoken as markup.
            buffer = re.sub(r'!?\[[^\]]*(?:\]\([^)]*)?$', '', buffer)
            buffer = re.sub(r'https?://\S*$', '', buffer)
            if buffer:
                await emit(len(buffer))
        return ''.join(parts)
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(iterator, 'aclose', None)
        if close is not None:
            await close()
