import asyncio

import pytest

from lira.audio.text_stream import consume_text


async def test_timeout_flushes_while_next_delta_is_suspended():
    spoken = asyncio.Event()
    seen = []
    async def source():
        yield '这是还没有标点的中文回答'
        await asyncio.wait_for(spoken.wait(), .2)
        yield '，接着说完。'
    async def speak(text):
        seen.append(text)
        spoken.set()
    raw = await consume_text(source(), speak, flush_seconds=.01)
    assert ''.join(seen) == raw


async def test_markup_numbers_and_words_survive_arbitrary_delta_boundaries():
    raw = '今天是2026年10月15日，温度3.14度。Visit [example](https://example.com/a.b) for extraordinary details。'
    seen = []
    async def source():
        for char in raw:
            yield char
    assert await consume_text(source(), seen.append) == raw
    joined = ''.join(seen)
    assert '3.14' in joined and '2026年10月15日' in joined
    assert 'extraordinary' in joined and 'example' in joined
    assert 'https' not in joined and '](' not in joined


async def test_callback_backpressure_and_cancellation_has_no_late_callback():
    entered = asyncio.Event()
    pulled = []
    async def source():
        for _ in range(10):
            pulled.append(1)
            yield '这是一段足够长的可播报文字。'
    async def speak(text):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(consume_text(source(), speak))
    await entered.wait()
    assert len(pulled) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unbreakable_input_has_deterministic_hard_limit():
    async def source():
        yield 'x' * 100
    with pytest.raises(ValueError, match='limit'):
        await consume_text(source(), lambda _: None, max_buffer_chars=64)


async def test_cancel_silent_source_closes_pending_read_and_timer():
    entered, closed = asyncio.Event(), asyncio.Event()
    seen = []
    async def source():
        try:
            yield 'incomplete'
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()
    task = asyncio.create_task(consume_text(source(), seen.append, flush_seconds=.01))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert closed.is_set() and seen == []


async def test_no_speech_consumer_retains_long_plain_response():
    async def source(): yield 'x' * 100
    assert await consume_text(source(), max_buffer_chars=16) == 'x' * 100


async def test_incremental_backend_receives_safe_prefix_without_sentence_wait():
    delivered = asyncio.Event()
    seen = []
    async def source():
        yield '你好'
        await asyncio.wait_for(delivered.wait(), .1)
        yield '，这是余下的内容。'
    def speak(text):
        seen.append(text)
        delivered.set()
    await consume_text(source(), speak, incremental=True)
    assert seen[0] == '你好'


@pytest.mark.parametrize('raw, expected', [
    ('**强调内容**。', '强调内容。'),
    ('*未闭合强调', '未闭合强调'),
    ('这是正文。[未闭合链接', '这是正文。'),
    ('```text\n代码内容\n```', '代码内容'),
])
async def test_cross_delta_markup_does_not_leak(raw, expected):
    seen = []
    async def source():
        for char in raw:
            yield char
    await consume_text(source(), seen.append)
    assert ''.join(seen) == expected


async def test_incremental_english_preserves_word_separators():
    seen = []
    async def source():
        yield 'Hello '
        yield 'wonderful '
        yield 'world.'
    await consume_text(source(), seen.append, incremental=True)
    assert ''.join(seen) == 'Hello wonderful world.'
