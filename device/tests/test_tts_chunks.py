import asyncio
from pathlib import Path
import numpy as np
from lira.audio.tts import speech_chunks
from test_audio_stack import playback_engine, HeldPlayer

def test_chunks_preserve_content_and_decimal_numbers():
    text = '上海明天气温为21.5度，建议带伞。' + '请注意天气变化。'*20
    chunks = list(speech_chunks(text))
    assert ''.join(chunks) == text
    assert len(chunks[0]) <= 32
    assert '21.5' in chunks[0]

def test_english_words_are_not_split():
    text = 'The weather tomorrow is sunny and comfortable for a short walk. '*5
    chunks = list(speech_chunks(text))
    assert ' '.join(chunks).split() == text.split()

def test_short_interjection_is_not_a_separate_chunk():
    chunks=list(speech_chunks('哦，查到了。今天最高二十度，最低十二度，中午可能下雨。'))
    assert len(chunks[0]) >= 10

async def test_first_chunk_plays_before_full_reply_and_cancel_stops_tail():
    import threading
    tail_started = threading.Event()
    tail_release = threading.Event()
    calls = []
    def generate(text, speed):
        calls.append(text)
        if len(calls) > 1:
            tail_started.set()
            tail_release.wait(2)
        return np.ones(8,dtype=np.float32)
    player = HeldPlayer()
    engine = playback_engine(player, generate)
    done = engine.speak('第一句很短。' + '后续内容需要更长时间合成。'*20, owner=1)
    await asyncio.wait_for(player.started.wait(), 1)
    assert not done.is_set()
    assert len(player.played) == 1
    assert len(calls[0]) <= 32
    await engine.stop(owner=1)
    tail_release.set()
    await asyncio.wait_for(engine.close(), 3)
    assert done.status == 'cancelled'
    assert len(player.played) == 1
