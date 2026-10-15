from types import SimpleNamespace

import numpy as np
import pytest

from lira.audio import tts
from lira.audio.tts import TtsEngine, to_pcm16
from lira.audio.voice_metrics import VoiceMetrics, current_trace


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    (tmp_path / 'model.onnx').touch()
    calls = []
    samples = np.array([0.25, -0.5], dtype=np.float32)
    def generate(text, sid, speed):
        calls.append((text, sid, speed))
        return SimpleNamespace(samples=samples.copy())
    monkeypatch.setattr(tts.sherpa_onnx, 'OfflineTts', lambda config:
                        SimpleNamespace(sample_rate=16000, generate=generate))
    engine = TtsEngine(model_dir=tmp_path, backend='melo')
    return engine, calls, samples


def test_feedback_ready_at_startup_and_uses_native_voice(loaded):
    engine, calls, samples = loaded
    assert calls == [('嗯。', 0, 1.0), ('嗯，我想想。', 0, 1.0)]
    for cue in ('嗯。', '嗯，我想想。'):
        output = engine.generate(cue)
        np.testing.assert_array_equal(output, samples)
        output[:] = 0
        np.testing.assert_array_equal(engine.generate(cue), samples)
    assert len(calls) == 2
    engine.generate('嗯。', speed=1.2)
    engine.generate('用户的普通回答')
    assert calls[-2:] == [('嗯。', 0, 1.2), ('用户的普通回答', 0, 1.0)]


@pytest.mark.asyncio
async def test_cached_feedback_keeps_playback_volume_cancel_and_metrics(loaded):
    engine, calls, samples = loaded
    played = []
    class Player:
        async def play_stream(self, source):
            async for pcm in source:
                played.append(pcm)
        async def close(self):
            pass
    engine._player = Player()
    metrics = VoiceMetrics(enabled=True)
    token = current_trace.set(metrics.trace(utterance_id=1))
    try:
        cancelled = engine.speak('嗯。', owner='old')
        engine.cancel_owner('old')
        done = engine.speak('嗯，我想想。', volume=0.4, owner='new')
        await done.wait()
    finally:
        current_trace.reset(token)
    assert cancelled.status == 'cancelled'
    assert done.status == 'success'
    assert played == [to_pcm16(samples, 0.4)]
    assert len(calls) == 2
    assert not any(row['event'].startswith('synthesis_') for row in metrics.snapshot())
    assert any(row['event'] == 'pcm_ready' for row in metrics.snapshot())
    assert metrics.summary()[0]['rtf'] is None
    await engine.close()
