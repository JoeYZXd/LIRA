from types import SimpleNamespace

import numpy as np
import pytest

from lira.audio import tts
from lira.audio.tts import TtsEngine


@pytest.fixture
def native_models(tmp_path, monkeypatch):
    model_dir = tmp_path / 'bilingual'
    model_dir.mkdir()
    (model_dir / 'model.onnx').touch()
    for name in ('phone.fst', 'date.fst', 'number.fst',
                 'phone-zh.fst', 'date-zh.fst', 'number-zh.fst'):
        (model_dir / name).touch()
    for name in ('LIRA_TTS_SPEAKER', 'LIRA_TTS_SPEAKER_ZH', 'LIRA_TTS_SPEAKER_EN'):
        monkeypatch.delenv(name, raising=False)
    configs, calls = [], []
    samples = np.array([0.1, -0.2, 0.3], dtype=np.float32)

    def load(config):
        configs.append(config)
        def generate(text, sid, speed):
            calls.append((text, sid, speed))
            return SimpleNamespace(samples=samples)
        return SimpleNamespace(sample_rate=44100, generate=generate)

    for name in ('OfflineTtsConfig', 'OfflineTtsModelConfig',
                 'OfflineTtsVitsModelConfig', 'OfflineTtsKokoroModelConfig'):
        monkeypatch.setattr(tts.sherpa_onnx, name, lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(tts.sherpa_onnx, 'OfflineTts', load)
    return model_dir, configs, calls, samples


@pytest.mark.parametrize('backend,sid', [('melo', 0), ('kokoro', 3)])
def test_bilingual_and_numbers_keep_one_model_and_voice(native_models, backend, sid):
    model_dir, configs, calls, samples = native_models
    engine = TtsEngine(model_dir=model_dir, backend=backend)
    texts = ['你好', 'Hello world', '今天学 English 123', '2026年10月1日', '13800138000']
    for text in texts:
        result = engine.generate(text, speed=1.2)
        np.testing.assert_array_equal(result, samples)
        assert result.dtype == np.float32
    assert calls == [(text, sid, 1.2) for text in texts]
    assert len(configs) == 1
    assert engine.sample_rate == 44100
    names = ('phone-zh.fst', 'date-zh.fst', 'number-zh.fst') if backend == 'kokoro' else ('phone.fst', 'date.fst', 'number.fst')
    assert configs[0].rule_fsts == ','.join(str(model_dir / name) for name in names)


@pytest.mark.parametrize('settings,sid', [
    ({'LIRA_TTS_SPEAKER_ZH': '7', 'LIRA_TTS_SPEAKER_EN': '1'}, 7),
    ({'LIRA_TTS_SPEAKER': '5', 'LIRA_TTS_SPEAKER_ZH': '7'}, 5),
])
def test_kokoro_explicit_voice_is_shared_across_languages(native_models, monkeypatch, settings, sid):
    model_dir, _, calls, _ = native_models
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    engine = TtsEngine(model_dir=model_dir, backend='kokoro')
    for text in ('你好', 'Hello', '123'):
        engine.generate(text)
    assert [call[1] for call in calls] == [sid] * 3
