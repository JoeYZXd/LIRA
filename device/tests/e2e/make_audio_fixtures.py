"""生成 e2e 音频夹具（U9）：离线 TTS（matcha zh-baker）合成指令/唤醒/白名单 wav。

一键再生（模型就绪时）::

    cd device && .venv/bin/python tests/e2e/make_audio_fixtures.py

产出 16kHz 单声道 16bit PCM wav 到 tests/e2e/audio_fixtures/，作为
MockAudioIO 注入源（"录制好的指令样本"）。夹具随仓库提交（小文件），
缺失时 e2e 用例 skipif 并提示运行本脚本。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

E2E_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = E2E_DIR / "audio_fixtures"
REPO_ROOT = E2E_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "device"))

from lira.audio._paths import (  # noqa: E402
    DOWNLOAD_HINT,
    SAMPLE_RATE,
    TTS_MODEL_DIR,
    VOCODER_ONNX,
)

#: 词条清单：(文件名, 文本)。唤醒词与白名单词用于真实 KWS 验证（AE1/AE4），
#: 其余用于真实 ASR 注入（识别失败时可降级文本注入，见各用例说明）。
UTTERANCES: list[tuple[str, str]] = [
    ("wake", "小丽拉"),
    ("open_heater", "打开取暖器"),
    ("confirm", "确认"),
    ("reject", "不要"),
    ("cancel", "取消"),
    ("pause", "暂停"),
    ("resume", "继续"),
    ("stop", "停止"),
    ("read_again", "再读一遍"),
    ("read_this", "帮我读一下这个"),
    ("volume_up", "加大音量"),
    ("volume_up_tv", "电视机调大音量"),
    ("weather", "今天天气怎么样"),
    ("open_lamp", "打开台灯"),
]

#: 前后静音垫（秒）：给 KWS/ASR 留出起止边界（词后 0.8s 长垫由注入器补）
LEAD_SECONDS = 0.15
TAIL_SECONDS = 0.15


def write_wav(path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


def resample(samples: np.ndarray, orig_sr: int, target_sr: int = SAMPLE_RATE) -> np.ndarray:
    n = int(len(samples) * target_sr / orig_sr)
    x_old = np.linspace(0.0, 1.0, len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def main() -> int:
    if not TTS_MODEL_DIR.is_dir() or not VOCODER_ONNX.is_file():
        print(f"TTS 模型未就绪，无法生成夹具。{DOWNLOAD_HINT}", file=sys.stderr)
        return 2
    from lira.audio.tts import TtsEngine

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    engine = TtsEngine(model_dir=TTS_MODEL_DIR, vocoder=VOCODER_ONNX, player=None)
    for name, text in UTTERANCES:
        samples = engine.generate(text, speed=1.0)
        pad = np.zeros(int(SAMPLE_RATE * (LEAD_SECONDS + TAIL_SECONDS)), dtype=np.float32)
        wav_samples = np.concatenate(
            [np.zeros(int(SAMPLE_RATE * LEAD_SECONDS), dtype=np.float32),
             resample(samples, engine.sample_rate), pad])
        out = FIXTURE_DIR / f"{name}.wav"
        write_wav(out, wav_samples[: len(wav_samples)])
        print(f"  {out.name}: {text} ({len(wav_samples) / SAMPLE_RATE:.2f}s)")
    print(f"完成：{len(UTTERANCES)} 个 wav -> {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
