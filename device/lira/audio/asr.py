"""流式 ASR（U2）：sherpa-onnx OnlineRecognizer.from_transducer 封装。

唤醒后的用户语音（16kHz mono float32）经 `AsrStream.feed` 持续喂入，
`text()` 随时取当前部分识别结果；endpoint 检测配合状态机切分一句话
（计划 Key Decisions：ASR 文本 → 3 级意图路由）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import sherpa_onnx

from lira.audio._paths import ASR_MODEL_DIR, DOWNLOAD_HINT, SAMPLE_RATE

__all__ = ["AsrStream", "StreamingAsr"]


class AsrStream:
    """单个流式解码流：喂波形 + 取部分文本，实现 MicDistributor 的 AudioSink 协议。"""

    def __init__(self, recognizer: sherpa_onnx.OnlineRecognizer) -> None:
        self._recognizer = recognizer
        self._stream = recognizer.create_stream()
        self.frames_fed = 0

    def feed(self, samples: np.ndarray) -> None:
        """接收 float32 波形（AudioSink 协议）。"""
        self._stream.accept_waveform(SAMPLE_RATE, samples)
        self.frames_fed += len(samples)

    def text(self) -> str:
        """解码全部就绪帧并返回当前累计识别文本（含部分结果）。"""
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        return self._recognizer.get_result(self._stream)

    def is_endpoint(self) -> bool:
        return self._recognizer.is_endpoint(self._stream)

    def reset(self) -> None:
        """endpoint 命中（一句话说完）后复位，开始下一句。"""
        self._recognizer.reset(self._stream)


class StreamingAsr:
    """流式 ASR 引擎：模型加载 + 流创建。"""

    def __init__(
        self,
        model_dir: str | Path = ASR_MODEL_DIR,
        num_threads: int = 1,
        enable_endpoint_detection: bool = True,
    ) -> None:
        model_dir = Path(model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"ASR 模型目录不存在: {model_dir}。{DOWNLOAD_HINT}")

        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(_pick(model_dir, "encoder")),
            decoder=str(_pick(model_dir, "decoder")),
            joiner=str(_pick(model_dir, "joiner")),
            num_threads=num_threads,
            decoding_method="greedy_search",
            enable_endpoint_detection=enable_endpoint_detection,
            rule1_min_trailing_silence=2.0,
            rule2_min_trailing_silence=0.8,
            rule3_min_utterance_length=20,
            provider="cpu",
        )
        logging.info("ASR 已加载: %s", model_dir.name)

    def create_stream(self) -> AsrStream:
        return AsrStream(self.recognizer)


def _pick(model_dir: Path, prefix: str) -> str:
    """从模型目录选 onnx：优先非 int8 全精度版本。"""
    candidates = sorted(model_dir.glob(f"{prefix}*.onnx"))
    full = [p for p in candidates if "int8" not in p.name]
    if not candidates:
        raise FileNotFoundError(f"{model_dir} 中找不到 {prefix}*.onnx。{DOWNLOAD_HINT}")
    return str((full or candidates)[-1])
