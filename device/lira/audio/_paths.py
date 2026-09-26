"""音频管线路径常量（内部模块，避免 __init__ 与子模块循环导入）。"""

from __future__ import annotations

from pathlib import Path

#: 全链路统一音频参数（计划 Key Decisions：16kHz 单声道 16bit PCM）
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # int16 字节宽
CHANNELS = 1

REPO_ROOT = Path(__file__).resolve().parents[3]
MODELS_DIR = REPO_ROOT / "models"

KWS_MODEL_DIR = MODELS_DIR / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
ASR_MODEL_DIR = MODELS_DIR / "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
TTS_MODEL_DIR = MODELS_DIR / "matcha-icefall-zh-baker"
#: vocos 声码器以裸 onnx 文件发布（vocoder-models release），非 tar 目录
VOCODER_ONNX = MODELS_DIR / "vocos-22khz-univ.onnx"

KEYWORDS_DIR = REPO_ROOT / "assets" / "keywords"
WAKEWORD_KEYWORDS_FILE = KEYWORDS_DIR / "wakeword_raw.txt"

DOWNLOAD_HINT = (
    "模型缺失，请先运行: python3 models/download_models.py"
)
