"""U2 音频管线测试（Test scenarios: wav 注入唤醒命中+reset / 1 分钟环境音零误触发 /
TTS 数字日期文本出音频+完成事件 / 麦克风分发双流无丢帧）。

测试不依赖声卡：全部经 MockAudioIO wav 注入 / 离线合成；
模型缺失时 skipif 并提示运行 models/download_models.py。
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest

from lira.audio import (
    ASR_MODEL_DIR,
    DOWNLOAD_HINT,
    KWS_MODEL_DIR,
    KEYWORDS_DIR,
    SAMPLE_RATE,
    TTS_MODEL_DIR,
    VOCODER_ONNX,
    MicDistributor,
    StreamingAsr,
    TtsEngine,
    WakeWordKws,
)
from lira.hal.mock import MockAudioIO

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
WAKEWORD = "小丽拉"

_kws_ready = KWS_MODEL_DIR.is_dir() and (KEYWORDS_DIR / "wakeword_raw.txt").is_file()
_asr_ready = ASR_MODEL_DIR.is_dir()
_tts_ready = TTS_MODEL_DIR.is_dir() and VOCODER_ONNX.is_file()

requires_kws = pytest.mark.skipif(not _kws_ready, reason=f"KWS 模型/关键词未就绪。{DOWNLOAD_HINT}")
requires_asr = pytest.mark.skipif(not _asr_ready, reason=f"ASR 模型未就绪。{DOWNLOAD_HINT}")
requires_tts = pytest.mark.skipif(not _tts_ready, reason=f"TTS 模型未就绪。{DOWNLOAD_HINT}")


# ---------- 工具 ----------

def make_wav(path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE) -> Path:
    """float32 [-1,1] 波形写 16bit PCM wav。"""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return path


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def resample(samples: np.ndarray, orig_sr: int, target_sr: int = SAMPLE_RATE) -> np.ndarray:
    """线性插值重采样（测试用足够；生产链路统一 16k）。"""
    n = int(len(samples) * target_sr / orig_sr)
    x_old = np.linspace(0.0, 1.0, len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class CountingSink:
    """计帧 sink：验证分发自播计数与 sink 实收一致（无丢帧）。"""

    def __init__(self) -> None:
        self.frames = 0

    def feed(self, samples: np.ndarray) -> None:
        self.frames += len(samples)


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def tts(tmp_path_factory) -> TtsEngine:
    return TtsEngine(
        model_dir=TTS_MODEL_DIR,
        vocoder=VOCODER_ONNX,
        player=MockAudioIO(tmp_path_factory.mktemp("silent") / "noop.wav"),
    )


@pytest.fixture(scope="module")
def wakeword_wav(tts: TtsEngine, tmp_path_factory) -> Path:
    """用 TTS 合成"小丽拉"发音并重采样到 16k，作为唤醒测试注入源。"""
    samples = tts.generate(WAKEWORD)
    assert len(samples) > 0
    return make_wav(
        tmp_path_factory.mktemp("wake") / "wakeword_16k.wav",
        resample(samples, tts.sample_rate),
    )


@pytest.fixture(scope="module")
def kws_engine() -> WakeWordKws:
    return WakeWordKws()


@pytest.fixture(scope="module")
def asr_engine() -> StreamingAsr:
    return StreamingAsr()


def feed_wav(stream, wav_path: Path, chunk_frames: int = 1600, tail_seconds: float = 0.7) -> None:
    """把 wav 按 0.1s 块喂给一个 KwsStream/AsrStream（AudioSink 协议）。

    KWS 命中依赖词后 trailing blanks（官方示例补 ~0.66s 静音），故默认补
    `tail_seconds` 静音尾巴；生产中真实麦克风天然提供词后静音。
    """
    samples = read_wav(wav_path)
    for start in range(0, len(samples), chunk_frames):
        stream.feed(samples[start : start + chunk_frames])
    if tail_seconds > 0:
        stream.feed(np.zeros(int(SAMPLE_RATE * tail_seconds), dtype=np.float32))


# ---------- Test scenario 1: wav 注入唤醒命中 + reset ----------

@pytest.mark.skipif(not (_kws_ready and _tts_ready), reason=f"KWS/TTS 模型未就绪。{DOWNLOAD_HINT}")
class TestWakeWordDetection:
    async def test_wav_injection_hits_and_reset_allows_next_hit(
        self, kws_engine: WakeWordKws, wakeword_wav: Path
    ):
        """Test scenario 1: 注入"小丽拉" wav 命中一次；reset 后同音频再次命中。"""
        stream = kws_engine.create_stream()
        feed_wav(stream, wakeword_wav)

        first = None
        for _ in range(200):
            hit = stream.poll()
            if hit:
                first = hit
                break
        assert first is not None, "注入唤醒词音频应至少命中一次"
        assert WAKEWORD in first
        assert stream.hits == 1

        # reset 已在 poll() 内完成：同一段音频再喂一遍应再次命中（流未坏）
        feed_wav(stream, wakeword_wav)
        second = None
        for _ in range(200):
            hit = stream.poll()
            if hit:
                second = hit
                break
        assert second is not None, "reset 后流应可继续检测"
        assert stream.hits == 2

    async def test_non_wakeword_speech_does_not_hit(
        self, kws_engine: WakeWordKws, tts: TtsEngine, tmp_path
    ):
        """非唤醒词语音（TTS 合成日常语句）不应触发唤醒。"""
        samples = tts.generate("今天天气怎么样")
        wav = make_wav(tmp_path / "speech_16k.wav", resample(samples, tts.sample_rate))
        stream = kws_engine.create_stream()
        feed_wav(stream, wav)
        for _ in range(100):
            assert stream.poll() is None
        assert stream.hits == 0


# ---------- Test scenario 2: 1 分钟环境音零误触发 ----------

@requires_kws
class TestAmbientNoFalseTrigger:
    async def test_one_minute_ambient_noise_zero_hits(
        self, kws_engine: WakeWordKws, tmp_path
    ):
        """Test scenario 2: 1 分钟环境噪声（合成白噪声，低幅）零误触发。"""
        rng = np.random.default_rng(42)
        ambient = (rng.standard_normal(SAMPLE_RATE * 60) * 0.03).astype(np.float32)
        wav = make_wav(tmp_path / "ambient_60s.wav", ambient)

        stream = kws_engine.create_stream()
        feed_wav(stream, wav)
        for _ in range(1000):
            assert stream.poll() is None, "环境噪声不应触发唤醒"
        assert stream.hits == 0


# ---------- Test scenario 3: TTS 数字/日期文本 ----------

@requires_tts
class TestTtsNumberAndDate:
    async def test_number_date_text_produces_audio_and_completion_event(
        self, tts: TtsEngine
    ):
        """Test scenario 3: 数字/日期/号码文本合成音频 >0；speak 完成事件 set；
        播放占用/释放 hook 按序广播（半双工保护）。"""
        text = "现在是2026年10月1日，请拨打13800138000。"
        samples = tts.generate(text)
        assert len(samples) > 0
        assert tts.sample_rate > 0

        events: list[str] = []
        player = MockAudioIO(Path("/dev/null"))  # 仅需 play 语义，不读文件
        engine = TtsEngine(
            model_dir=TTS_MODEL_DIR,
            vocoder=VOCODER_ONNX,
            player=player,
            on_occupied=lambda: events.append("occupied"),
            on_released=lambda: events.append("released"),
        )
        done = engine.speak(text, speed=1.0, volume=1.0)
        await asyncio.wait_for(done.wait(), timeout=60)

        assert done.is_set(), "speak 完成事件应被置位"
        assert events == ["occupied", "released"], "播放期占用/释放 hook 应按序广播"
        assert engine.occupied is False
        assert len(player.played) == 1
        assert len(player.played[0]) > 0
        # 16bit PCM 字节数为采样点数的 2 倍；matcha 采样有随机性，
        # speak() 内部重新合成的长度与 generate() 预览略有出入，只约束量级
        assert len(player.played[0]) % 2 == 0
        assert abs(len(player.played[0]) // 2 - len(samples)) < SAMPLE_RATE

    async def test_speed_up_produces_shorter_audio(self, tts: TtsEngine):
        text = "小丽拉已就绪"
        normal = tts.generate(text, speed=1.0)
        fast = tts.generate(text, speed=1.5)
        assert 0 < len(fast) < len(normal)


# ---------- Test scenario 4: 麦克风分发双流无丢帧 ----------

class TestMicDistributor:
    async def test_dual_sink_counts_match_no_frame_loss(self, tmp_path):
        """Test scenario 4: 分发器喂双 sink，各 sink 帧数 == 源总帧数。"""
        rng = np.random.default_rng(7)
        total_frames = SAMPLE_RATE * 2  # 2 秒
        wav = make_wav(tmp_path / "src.wav", rng.standard_normal(total_frames).astype(np.float32) * 0.1)

        source = MockAudioIO(wav)
        dist = MicDistributor(source)
        sink_a, sink_b = CountingSink(), CountingSink()
        dist.add_sink(sink_a)
        dist.add_sink(sink_b)

        async with source:
            await dist.run()

        assert dist.frames_total == total_frames
        assert sink_a.frames == total_frames
        assert sink_b.frames == total_frames
        assert dist.frames_per_sink == [total_frames, total_frames]

    @pytest.mark.skipif(not (_kws_ready and _asr_ready and _tts_ready), reason=f"KWS/ASR/TTS 模型未就绪。{DOWNLOAD_HINT}")
    async def test_feeds_kws_and_asr_streams_simultaneously(
        self, kws_engine: WakeWordKws, asr_engine: StreamingAsr, wakeword_wav: Path
    ):
        """分发器同时喂真实 KWS + ASR 流，两路帧数一致且无丢帧。"""
        source = MockAudioIO(wakeword_wav)
        dist = MicDistributor(source)
        kws_stream = kws_engine.create_stream()
        asr_stream = asr_engine.create_stream()
        dist.add_sink(kws_stream)
        dist.add_sink(asr_stream)

        async with source:
            await dist.run()

        total = dist.frames_total
        assert total == kws_stream.frames_fed == asr_stream.frames_fed > 0
        assert dist.frames_per_sink == [total, total]

    async def test_eof_stops_distribution(self, tmp_path):
        wav = make_wav(tmp_path / "short.wav", np.zeros(SAMPLE_RATE // 10, dtype=np.float32))
        source = MockAudioIO(wav)
        dist = MicDistributor(source, chunk_frames=160)
        sink = CountingSink()
        dist.add_sink(sink)
        async with source:
            await dist.run()
        assert dist.frames_total == SAMPLE_RATE // 10
        assert sink.frames == dist.frames_total


# ---------- 加载错误路径（不依赖模型） ----------

class TestLoadErrors:
    def test_missing_model_dir_raises_with_hint(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="download_models"):
            WakeWordKws(keywords_file=tmp_path / "kw.txt", model_dir=tmp_path / "nope")

    def test_missing_keywords_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="关键词文件"):
            WakeWordKws(keywords_file=tmp_path / "kw.txt", model_dir=MODELS_DIR)

    def test_missing_asr_model_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="download_models"):
            StreamingAsr(model_dir=tmp_path / "nope")

    def test_missing_tts_model_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="download_models"):
            TtsEngine(model_dir=tmp_path / "nope")
