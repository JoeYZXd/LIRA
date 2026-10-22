"""LIRA 音频管线（U2）：麦克风分发 / KWS 唤醒 / 流式 ASR / TTS 合成。

模型目录约定（models/download_models.py 下载，gitignore 不入库）::

    models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/          KWS 唤醒
    models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/  流式 ASR
    models/matcha-icefall-zh-baker/ + models/vocos-22khz-univ.onnx  TTS
    assets/keywords/                                               关键词文件

路径常量见 `_paths`（构造函数均可覆盖，U10 板上重定位时不改代码）。
"""

from __future__ import annotations

from lira.audio._paths import (
    ASR_MODEL_DIR,
    CHANNELS,
    DOWNLOAD_HINT,
    KEYWORDS_DIR,
    KWS_MODEL_DIR,
    MODELS_DIR,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    TTS_MODEL_DIR,
    VOCODER_ONNX,
    WAKEWORD_KEYWORDS_FILE,
)
from importlib import import_module

# Text and timing helpers are also used without the optional audio extra.
_ENGINES = {
    "AsrStream": "asr", "StreamingAsr": "asr",
    "KwsStream": "kws", "WakeWordKws": "kws",
    "MicDistributor": "mic", "SoundDeviceMic": "mic", "TtsEngine": "tts",
}


def __getattr__(name):
    module = _ENGINES.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value

__all__ = [
    "ASR_MODEL_DIR",
    "CHANNELS",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "KWS_MODEL_DIR",
    "KEYWORDS_DIR",
    "MODELS_DIR",
    "DOWNLOAD_HINT",
    "TTS_MODEL_DIR",
    "VOCODER_ONNX",
    "WAKEWORD_KEYWORDS_FILE",
    "AsrStream",
    "KwsStream",
    "MicDistributor",
    "SoundDeviceMic",
    "StreamingAsr",
    "TtsEngine",
    "WakeWordKws",
]
