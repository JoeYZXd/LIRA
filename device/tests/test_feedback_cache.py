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


@pytest.mark.parametrize('invalid', [
    np.array([], dtype=np.float32),
    np.array([0.0, np.nan], dtype=np.float32),
    np.array([np.inf], dtype=np.float32),
    np.zeros((2, 2), dtype=np.float32),
    np.array(0.25, dtype=np.float32),
], ids=['empty', 'nan', 'infinite', 'matrix', 'scalar'])
def test_invalid_feedback_audio_prevents_startup(tmp_path, monkeypatch, invalid):
    (tmp_path / 'model.onnx').touch()
    monkeypatch.setattr(tts.sherpa_onnx, 'OfflineTts', lambda config:
                        SimpleNamespace(sample_rate=16000, generate=lambda *args, **kwargs:
                                        SimpleNamespace(samples=invalid)))
    with pytest.raises(RuntimeError, match='warmup produced invalid audio'):
        TtsEngine(model_dir=tmp_path, backend='melo')


@pytest.mark.parametrize('oversized_cues', [
    ('嗯。',), ('嗯。', '嗯，我想想。'),
], ids=['one-cue', 'both-cues'])
def test_long_feedback_bypasses_cache_without_caching_other_text(
        tmp_path, monkeypatch, oversized_cues):
    (tmp_path / 'model.onnx').touch()
    calls = []
    normal = np.array([0.25, -0.5], dtype=np.float32)
    long_audio = np.full(16000 * 4 + 1, 0.125, dtype=np.float32)

    def generate(text, sid, speed):
        calls.append((text, sid, speed))
        return SimpleNamespace(samples=(long_audio if text in oversized_cues else normal).copy())

    monkeypatch.setattr(tts.sherpa_onnx, 'OfflineTts', lambda config:
                        SimpleNamespace(sample_rate=16000, generate=generate))
    engine = TtsEngine(model_dir=tmp_path, backend='melo')
    assert calls == [('嗯。', 0, 1.0), ('嗯，我想想。', 0, 1.0)]
    for cue in ('嗯。', '嗯，我想想。'):
        before = len(calls)
        expected = long_audio if cue in oversized_cues else normal
        output = engine.generate(cue)
        np.testing.assert_array_equal(output, expected)
        output[:] = 0
        np.testing.assert_array_equal(engine.generate(cue), expected)
        assert len(calls) - before == (2 if cue in oversized_cues else 0)
    before = len(calls)
    for _ in range(2):
        np.testing.assert_array_equal(engine.generate('普通回答'), normal)
    assert calls[before:] == [('普通回答', 0, 1.0)] * 2


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
