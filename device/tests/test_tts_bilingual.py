from types import SimpleNamespace
import numpy as np
import pytest
from lira.audio.tts import TtsEngine

@pytest.mark.parametrize('text,sid', [('你好',3), ('Hello world',0), ('今天学 English',3)])
def test_kokoro_selects_voice_for_language(text, sid):
    engine = TtsEngine.__new__(TtsEngine)
    engine._backend = 'kokoro'
    engine._speaker_zh, engine._speaker_en = 3, 0
    calls = []
    def generate(text, sid, speed):
        calls.append((sid, speed))
        return SimpleNamespace(samples=[0.1,0.2])
    engine.tts = SimpleNamespace(generate=generate)
    assert engine.generate(text).dtype == np.float32
    assert calls == [(sid,1.0)]
