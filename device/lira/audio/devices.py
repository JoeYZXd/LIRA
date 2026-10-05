"""Fail closed on explicit board audio routing; never choose substring matches."""
import json
import os
import subprocess
from lira.config import ConfigError


def board_audio_device(value, *, direction, echo_cancel=False):
    if echo_cancel:
        if value != "lira_audio":
            raise ConfigError("AEC requires lira_audio for both audio directions")
        if any(os.environ.get(key) for key in ("PIPEWIRE_NODE", "PIPEWIRE_ALSA")):
            raise ConfigError("Remove PIPEWIRE_NODE / PIPEWIRE_ALSA overrides before AEC startup")
        try:
            nodes = json.loads(subprocess.check_output(["pw-dump"], timeout=5, text=True))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ConfigError("Cannot inspect AEC nodes using pw-dump") from exc
        found = {node.get("info", {}).get("props", {}).get("node.name"):
                 node.get("info", {}).get("props", {}).get("media.class") for node in nodes}
        if found.get("lira_echo_source") != "Audio/Source" or found.get("lira_echo_sink") != "Audio/Sink":
            raise ConfigError("LIRA AEC source/sink absent; install the dedicated PipeWire configuration")
    if not value:
        return None
    import sounddevice as sd
    devices = sd.query_devices()
    field = "max_input_channels" if direction == "input" else "max_output_channels"
    if value.isdigit():
        index = int(value)
        if index >= len(devices) or devices[index][field] < 1:
            raise ConfigError("Configured audio device index is unavailable")
        return index
    matches = [i for i, device in enumerate(devices)
               if device["name"] == value and device[field] > 0
               and sd.query_hostapis(device["hostapi"])["name"] == "ALSA"]
    if len(matches) != 1:
        raise ConfigError(f"Audio device {value!r} must resolve to one exact {direction} PCM")
    return matches[0]
