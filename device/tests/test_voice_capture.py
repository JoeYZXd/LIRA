from types import SimpleNamespace

import asyncio
import inspect
import numpy as np

from lira.dialog.route_dispatch import RouteDispatch
from lira.dialog.state_machine import AudioRoute


class Stream:
    def __init__(self):
        self.samples = []
        self.hit = None
        self.result = ""

    def feed(self, samples):
        self.samples.extend(samples)

    def feed_and_poll(self, samples):
        hit, self.hit = self.hit, None
        return hit

    def reset(self):
        self.samples.clear()

    def text(self):
        return self.result

    def is_endpoint(self):
        return bool(self.result)


def rig():
    events = []
    async def asr(text):
        events.append(text)
    def spawn(event):
        if inspect.isawaitable(event):
            asyncio.run(event)
        else:
            events.append(event)
    holder = SimpleNamespace(route=AudioRoute.WAKE, wake_stream=Stream(),
        asr_stream=Stream(), playback_stream=Stream(), tts=SimpleNamespace(occupied=True),
        engine=SimpleNamespace(on_wake=lambda: "wake", on_asr_text=asr))
    return holder, RouteDispatch(streams=holder, wakeword="小丽拉", spawn=spawn), events


def test_delayed_wake_replays_chronologically_once_even_during_tts():
    holder, dispatch, events = rig()
    holder.wake_stream.hit = "小丽拉"
    dispatch.feed(np.array([1, 2], dtype=np.float32))
    dispatch.feed(np.array([3, 4], dtype=np.float32))
    assert events == ["wake"]
    holder.route = AudioRoute.LISTEN
    dispatch.begin_listening()
    dispatch.feed(np.array([5, 6], dtype=np.float32))
    assert holder.asr_stream.samples == [1, 2, 3, 4, 5, 6]


def test_clear_removes_pre_privacy_audio_and_recognizer_residual():
    holder, dispatch, _ = rig()
    dispatch.feed(np.array([1, 2], dtype=np.float32))
    dispatch.clear()
    holder.route = AudioRoute.LISTEN
    dispatch.begin_listening()
    assert holder.asr_stream.samples == []


def test_wake_remains_active_during_playback_and_only_prefix_removed():
    holder, dispatch, events = rig()
    holder.route = AudioRoute.PLAYBACK
    holder.wake_stream.hit = "小丽拉"
    dispatch.feed(np.zeros(1600, dtype=np.float32))
    assert events == ["wake"]
    holder.route = AudioRoute.LISTEN
    dispatch.begin_listening()
    holder.asr_stream.result = "小丽拉，讲讲小丽拉的名字"
    dispatch.feed(np.zeros(1600, dtype=np.float32))
    assert events[-1] == "讲讲小丽拉的名字"


def test_activity_onset_and_silence_end_are_distinct_from_asr_result():
    from lira.audio.activity import SpeechActivity
    detector = SpeechActivity()
    assert detector.feed(np.full(1600, 0.03, dtype=np.float32)) == ["started"]
    assert detector.active
    assert detector.feed(np.zeros(11200, dtype=np.float32)) == ["ended"]
    assert not detector.active


def test_long_active_speech_is_segmented_but_not_submitted():
    holder, dispatch, events = rig()
    holder.route = AudioRoute.LISTEN
    holder.asr_stream.result = "长句前半"
    dispatch.feed(np.full(320000, 0.03, dtype=np.float32))
    assert events == []
    holder.asr_stream.result = "长句后半"
    dispatch.feed(np.zeros(11200, dtype=np.float32))
    assert events == ["长句前半长句后半"]


def test_preroll_is_bounded_to_three_seconds():
    holder, dispatch, _ = rig()
    dispatch.feed(np.zeros(64000, dtype=np.float32))
    holder.route = AudioRoute.LISTEN
    dispatch.begin_listening()
    assert len(holder.asr_stream.samples) == 48000


def test_aec_resolver_rejects_override_and_missing_nodes(monkeypatch):
    from lira.audio.devices import board_audio_device
    from lira.config import ConfigError
    import pytest
    monkeypatch.setenv("PIPEWIRE_NODE", "wrong")
    with pytest.raises(ConfigError, match="overrides"):
        board_audio_device("lira_audio", direction="input", echo_cancel=True)
    monkeypatch.delenv("PIPEWIRE_NODE")
    monkeypatch.delenv("PIPEWIRE_ALSA", raising=False)
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: "[]")
    with pytest.raises(ConfigError, match="absent"):
        board_audio_device("lira_audio", direction="input", echo_cancel=True)


def test_audio_resolver_does_not_use_substring_fallback(monkeypatch):
    import sys
    import pytest
    from lira.audio.devices import board_audio_device
    from lira.config import ConfigError
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: [
        {"name": "pipewire", "max_input_channels": 2, "max_output_channels": 2, "hostapi": 0},
        {"name": "lira_audio", "max_input_channels": 1, "max_output_channels": 1, "hostapi": 0}],
        query_hostapis=lambda index: {"name": "ALSA"}))
    assert board_audio_device("lira_audio", direction="input") == 1
    with pytest.raises(ConfigError, match="exact"):
        board_audio_device("pipe", direction="input")


async def test_mic_overflow_invalidates_old_samples_and_close_unblocks_reader(monkeypatch):
    import sys
    import pytest
    from lira.audio.mic import SoundDeviceMic, AudioInputError
    callbacks = []
    class Input:
        def __init__(self, **kwargs):
            callbacks.append(kwargs["callback"])
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Input))
    mic = SoundDeviceMic()
    await mic.open()
    for _ in range(101):
        callbacks[0](b"\x01\x00", 1, None, False)
    await asyncio.sleep(0)
    with pytest.raises(AudioInputError):
        await mic.read_chunk(2)
    reader = asyncio.create_task(mic.read_chunk(2))
    await asyncio.sleep(0)
    await mic.close()
    assert await reader == b""
    await mic.open()
    callbacks[0](b"old", 1, None, False)
    callbacks[1](b"new", 1, None, False)
    await asyncio.sleep(0)
    assert await mic.read_chunk(2) == b"new"
    await mic.close()


async def test_queued_asr_delivery_cannot_enter_replacement_generation():
    holder, dispatch, events = rig()
    holder.route = AudioRoute.LISTEN
    pending = []
    dispatch._spawn = pending.append
    holder.asr_stream.result = "旧问题"
    dispatch.feed(np.zeros(1600, dtype=np.float32))
    dispatch.clear()
    await pending[0]
    assert events == []


def test_aec_resolver_requires_node_media_classes(monkeypatch):
    import json
    import pytest
    from lira.audio.devices import board_audio_device
    from lira.config import ConfigError
    monkeypatch.delenv("PIPEWIRE_NODE", raising=False)
    monkeypatch.delenv("PIPEWIRE_ALSA", raising=False)
    nodes = [{"info": {"props": {"node.name": name, "media.class": "Audio/Sink"}}}
             for name in ("lira_echo_source", "lira_echo_sink")]
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: json.dumps(nodes))
    with pytest.raises(ConfigError, match="absent"):
        board_audio_device("lira_audio", direction="input", echo_cancel=True)


def test_exact_named_pcm_requires_alsa_hostapi(monkeypatch):
    import sys
    import pytest
    from lira.audio.devices import board_audio_device
    from lira.config import ConfigError
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: [
        {"name": "lira_audio", "max_input_channels": 1, "max_output_channels": 1, "hostapi": 0}],
        query_hostapis=lambda index: {"name": "JACK"}))
    with pytest.raises(ConfigError, match="exact"):
        board_audio_device("lira_audio", direction="input")
