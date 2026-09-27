"""U1 mock HAL 测试（Test scenarios: 连拍不同图 / IR 码值入内存列表 / wav 音频）。"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from lira.hal import HalError
from lira.hal.mock import (
    MockAudioIO,
    MockCamera,
    MockDisplay,
    MockIrController,
)

ASSETS = Path(__file__).resolve().parents[2] / "assets"


def make_image_dir(tmp_path: Path, count: int = 2) -> Path:
    d = tmp_path / "images"
    d.mkdir()
    for i in range(count):
        (d / f"img_{i}.png").write_bytes(f"PNG-CONTENT-{i}".encode())
    return d


def make_wav(tmp_path: Path, seconds: float = 0.1, rate: int = 16000) -> Path:
    path = tmp_path / "in.wav"
    import struct

    frames = b"".join(struct.pack("<h", (i % 1000) * 10) for i in range(int(rate * seconds)))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)
    return path


class TestMockCamera:
    async def test_two_consecutive_captures_return_different_content(self, tmp_path):
        """Test scenario 3: 连拍两图返回不同文件内容。"""
        async with MockCamera(make_image_dir(tmp_path, count=2)) as cam:
            first = await cam.capture()
            second = await cam.capture()
            assert first != second

    async def test_cycles_back_to_first_image(self, tmp_path):
        async with MockCamera(make_image_dir(tmp_path, count=2)) as cam:
            seq = [await cam.capture() for _ in range(4)]
            assert seq[0] == seq[2]
            assert seq[1] == seq[3]
            assert cam.capture_count == 4

    async def test_reads_real_asset_images(self):
        """仓库自带 mock 样张可被读取（main --dry-run 依赖此路径）。"""
        async with MockCamera(ASSETS / "mock_images") as cam:
            assert len(cam.image_names) >= 2
            data = await cam.capture()
            assert data.startswith(b"\x89PNG")

    async def test_empty_dir_raises_hal_error(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        cam = MockCamera(empty)
        with pytest.raises(HalError, match="为空|不存在"):
            await cam.open()

    async def test_capture_before_open_raises(self, tmp_path):
        cam = MockCamera(make_image_dir(tmp_path))
        with pytest.raises(HalError):
            await cam.capture()


class TestMockIr:
    async def test_send_records_code_to_memory_list(self):
        """Test scenario 3: mock IR send 记录码值到内存列表。"""
        ir = MockIrController()
        async with ir:
            await ir.send("pulse:9000,space:4500,pulse:560")
            await ir.send("pulse:2400,space:600")
        assert ir.sent_codes == ["pulse:9000,space:4500,pulse:560", "pulse:2400,space:600"]
        assert ir.send_count == 2

    async def test_learn_pops_from_queue(self):
        ir = MockIrController()
        ir.learn_queue = ["pulse:1,space:2"]
        async with ir:
            code = await ir.learn(timeout_seconds=1.0)
        assert code == "pulse:1,space:2"

    async def test_learn_empty_queue_times_out(self):
        ir = MockIrController()
        async with ir:
            with pytest.raises(HalError, match="超时"):
                await ir.learn(timeout_seconds=0.01)

    async def test_send_empty_code_rejected(self):
        ir = MockIrController()
        async with ir:
            with pytest.raises(HalError):
                await ir.send("")


class TestMockAudio:
    async def test_reads_pcm_chunks_from_wav(self, tmp_path):
        wav = make_wav(tmp_path, seconds=0.1)  # 1600 frames = 3200 bytes
        audio = MockAudioIO(wav)
        async with audio:
            chunk = await audio.read_chunk(800)  # 请求 800 字节 → 400 帧 = 800 字节
            assert len(chunk) == 800
            assert audio.sample_rate == 16000
            assert audio.channels == 1

    async def test_wav_exhaustion_returns_empty(self, tmp_path):
        audio = MockAudioIO(make_wav(tmp_path, seconds=0.01))  # 160 帧 = 320 字节
        async with audio:
            total = b""
            while True:
                chunk = await audio.read_chunk(64000)
                if not chunk:
                    break
                total += chunk
            assert len(total) == 320
            assert await audio.read_chunk(100) == b""  # EOF 之后持续为空

    async def test_play_records_to_memory(self, tmp_path):
        audio = MockAudioIO(make_wav(tmp_path))
        async with audio:
            await audio.play(b"\x01\x02")
            await audio.play(b"\x03\x04")
        assert audio.played == [b"\x01\x02", b"\x03\x04"]

    async def test_missing_wav_raises(self, tmp_path):
        audio = MockAudioIO(tmp_path / "nope.wav")
        with pytest.raises(HalError, match="不存在"):
            await audio.open()


class TestMockDisplay:
    async def test_display_records_shown_and_cleared(self):
        async with MockDisplay() as display:
            await display.show("小丽拉已就绪\n音量 60")
            await display.clear()
            await display.show("隐私模式")
        assert display.shown == ["小丽拉已就绪\n音量 60", "<cleared>", "隐私模式"]
        assert display.current == "隐私模式"

    async def test_display_empty_show_rejected(self):
        async with MockDisplay() as display:
            with pytest.raises(HalError):
                await display.show("")


class TestContextManagers:
    async def test_all_mocks_are_async_context_managers(self, tmp_path):
        """HAL 约定：接口均为 async 上下文管理器，退出后释放资源。"""
        async with (
            MockCamera(make_image_dir(tmp_path)) as camera,
            MockAudioIO(make_wav(tmp_path)) as audio,
            MockIrController() as ir,
            MockDisplay() as display,
        ):
            assert await camera.capture()
            assert isinstance(await audio.read_chunk(4), bytes)
            await ir.send("pulse:1")
            await display.show("ok")

        # 退出后资源已释放
        with pytest.raises(HalError):
            await camera.capture()
        with pytest.raises(HalError):
            await audio.read_chunk(4)
