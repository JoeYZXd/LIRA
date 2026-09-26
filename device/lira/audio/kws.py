"""KWS 唤醒词检测（U2）：sherpa-onnx KeywordSpotter 封装，支持多实例。

同一模型可创建多个实例（计划 U2 Approach）：
  - 常驻唤醒词实例（"小丽拉"，assets/keywords/wakeword_raw.txt）；
  - U3 播放白名单实例（{暂停、继续、停止、再读一遍、大声点、小声点}）。
  - 实例间互不共享 stream；命中后必须 reset_stream（文档明确要求），
    `KwsStream.poll()` 已内置。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import sherpa_onnx

from lira.audio._paths import (
    DOWNLOAD_HINT,
    KWS_MODEL_DIR,
    SAMPLE_RATE,
    WAKEWORD_KEYWORDS_FILE,
)

__all__ = ["KwsStream", "WakeWordKws"]


def _pick_onnx(model_dir: Path, prefix: str) -> Path:
    """从模型目录选 onnx：优先非 int8 全精度版本。"""
    candidates = sorted(model_dir.glob(f"{prefix}*.onnx"))
    full = [p for p in candidates if "int8" not in p.name]
    chosen = (full or candidates or [None])[-1] if candidates else None
    if chosen is None:
        raise FileNotFoundError(f"{model_dir} 中找不到 {prefix}*.onnx。{DOWNLOAD_HINT}")
    return chosen


class KwsStream:
    """单个 KWS 解码流：喂波形 + 轮询命中，实现 MicDistributor 的 AudioSink 协议。"""

    def __init__(self, engine: "WakeWordKws") -> None:
        self._engine = engine
        self._stream = engine.kws.create_stream()
        #: 累计命中次数（测试断言用）
        self.hits = 0
        self.frames_fed = 0

    def feed(self, samples: np.ndarray) -> None:
        """接收 float32 波形（AudioSink 协议）。"""
        self._stream.accept_waveform(SAMPLE_RATE, samples)
        self.frames_fed += len(samples)

    def poll(self) -> str | None:
        """解码就绪帧并返回命中关键词文本；未命中返回 None。

        命中后立即 reset_stream（sherpa-onnx 官方要求），流可继续检测下一次。
        """
        kws = self._engine.kws
        while kws.is_ready(self._stream):
            kws.decode_stream(self._stream)
            result = kws.get_result(self._stream)
            if result:
                kws.reset_stream(self._stream)
                self.hits += 1
                logging.info("KWS 命中: %s (第 %d 次)", result, self.hits)
                return result
        return None

    def feed_and_poll(self, samples: np.ndarray) -> str | None:
        """喂一段波形并轮询（分发 sink 的常用组合）。"""
        self.feed(samples)
        return self.poll()


class WakeWordKws:
    """KeywordSpotter 单实例封装：模型加载 + 流创建。

    同一模型目录可实例化多个 WakeWordKws（不同 keywords_file）实现多关键词组。
    """

    def __init__(
        self,
        keywords_file: str | Path = WAKEWORD_KEYWORDS_FILE,
        model_dir: str | Path = KWS_MODEL_DIR,
        keywords_threshold: float = 0.25,
        num_threads: int = 1,
    ) -> None:
        model_dir = Path(model_dir)
        keywords_file = Path(keywords_file)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"KWS 模型目录不存在: {model_dir}。{DOWNLOAD_HINT}")
        if not keywords_file.is_file():
            raise FileNotFoundError(f"关键词文件不存在: {keywords_file}")

        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(_pick_onnx(model_dir, "encoder")),
            decoder=str(_pick_onnx(model_dir, "decoder")),
            joiner=str(_pick_onnx(model_dir, "joiner")),
            keywords_file=str(keywords_file),
            keywords_threshold=keywords_threshold,
            num_threads=num_threads,
            provider="cpu",
        )
        logging.info("KWS 已加载: model=%s keywords=%s", model_dir.name, keywords_file.name)

    def create_stream(self) -> KwsStream:
        return KwsStream(self)
