import numpy as np
import pytest
from lira.diagnostics.camera_quality import (
    parse_format,
    decode_nv12,
    split_frames,
    parse_controls,
)

FMT = """Format Video Capture Multiplanar:
 Width/Height      : 4/4
 Pixel Format      : 'NV12'
 Number of planes  : 1
 Plane 0:
  Bytes per Line : 6
  Size Image : 40
"""


def test_padded_nv12_and_payload_boundaries():
    fmt = parse_format(FMT)
    tight = np.arange(24, dtype=np.uint8).reshape(6, 4)
    padded = np.full((6, 6), 255, np.uint8)
    padded[:, :4] = tight
    assert np.array_equal(
        decode_nv12(padded.tobytes(), fmt),
        decode_nv12(tight.tobytes(), {**fmt, "bytesperline": 4, "sizeimage": 24}),
    )
    assert split_frames(padded.tobytes() * 3, [36] * 3, 3) == [padded.tobytes()] * 3
    with pytest.raises(ValueError):
        split_frames(padded.tobytes(), [40], 1)
    with pytest.raises(ValueError):
        split_frames(padded.tobytes(), [36], 3)


def test_reject_ambiguous_formats_and_short_data():
    with pytest.raises(ValueError):
        parse_format(FMT.replace("NV12", "NV12M"))
    with pytest.raises(ValueError):
        parse_format(FMT.replace(": 1", ": 2"))
    with pytest.raises(ValueError):
        decode_nv12(b"123", parse_format(FMT))


def test_controls_keep_range_and_current():
    c = parse_controls(
        " exposure 0x00980911 (int) : min=4 max=6400 step=1 default=100 value=400\n analogue_gain 0x009e0903 (int) : min=128 max=1984 step=1 default=128 value=384\n vertical_blanking 0x009e0901 (int) : min=12 max=32767 step=1 default=12 value=3300"
    )
    assert c["exposure"]["value"] == 400
    assert c["analogue_gain"]["min"] == 128


import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from lira.diagnostics.camera_quality import (
    CameraSession,
    DeviceLock,
    external_owners,
    run_process,
    capture,
)


class FakeV4l:
    def __init__(self, fail=None, clamp=False):
        self.values = {"vertical_blanking": 3300, "exposure": 400, "analogue_gain": 384}
        self.calls = []
        self.fail = fail
        self.clamp = clamp
        self.format = FMT

    async def __call__(self, args, timeout):
        arg = args[-1]
        self.calls.append(arg)
        if arg == "--get-fmt-video":
            return self.format
        if arg == "--list-ctrls":
            return "\n".join(
                f"{k} 0x00980911 (int) : min={128 if k == 'analogue_gain' else 4} max=32767 step=1 default=128 value={v}"
                for k, v in self.values.items()
            )
        if arg.startswith("--set-ctrl="):
            key, value = arg.split("=", 1)[1].split("=")
            if self.fail == key:
                self.fail = None
                raise RuntimeError("write failed")
            self.values[key] = (
                int(value) - 1 if self.clamp and key == "exposure" else int(value)
            )
        return ""


def test_partial_setup_restores_dependency_order():
    async def scenario():
        fake = FakeV4l(fail="analogue_gain")
        m = {}
        session = CameraSession("video", "sensor", m, fake)
        await session.snapshot()
        try:
            await session.setup(
                SimpleNamespace(vblank=5000, exposure=900, gain=500, width=None)
            )
        except RuntimeError:
            pass
        finally:
            await session.restore()
        assert fake.values == {
            "vertical_blanking": 3300,
            "exposure": 400,
            "analogue_gain": 384,
        }
        assert m["restore"]["ok"]
        writes = [x for x in fake.calls if x.startswith("--set-ctrl=")]
        assert writes[-4:] == [
            "--set-ctrl=exposure=4",
            "--set-ctrl=vertical_blanking=3300",
            "--set-ctrl=exposure=400",
            "--set-ctrl=analogue_gain=384",
        ]

    asyncio.run(scenario())


def test_clamped_readback_and_restore_failure_are_recorded():
    async def scenario():
        fake = FakeV4l(clamp=True)
        m = {}
        s = CameraSession("v", "s", m, fake)
        await s.snapshot()
        await s.setup(SimpleNamespace(vblank=None, exposure=900, gain=None, width=None))
        assert m["readback"]["exposure"]["value"] == 899
        await s.restore()
        assert not m["restore"]["ok"]
        assert any("differs" in x for x in m["restore"]["errors"])

    asyncio.run(scenario())


def test_lock_and_external_owners(tmp_path):
    with DeviceLock(tmp_path / "lock"):
        with pytest.raises(RuntimeError):
            with DeviceLock(tmp_path / "lock"):
                pass
    proc = tmp_path / "proc"
    pid = proc / "99999"
    (pid / "fd").mkdir(parents=True)
    (pid / "comm").write_text("ispserver")
    (pid / "cmdline").write_bytes(b"ispserver\x00")
    assert external_owners(["/dev/video11"], proc)[0]["pid"] == 99999
    (pid / "comm").write_text("capture")
    (pid / "cmdline").write_bytes(b"capture\x00")
    (pid / "fd" / "3").symlink_to("/dev/video11")
    assert external_owners(["/dev/video11"], proc)[0]["nodes"] == ["/dev/video11"]


def test_timeout_and_cancel_reap_child_process_group(tmp_path):
    async def scenario(cancel):
        childfile = tmp_path / ("cancel" if cancel else "timeout")
        script = 'import subprocess,time,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); open(sys.argv[1],"w").write(str(p.pid)); time.sleep(60)'
        task = asyncio.create_task(
            run_process(
                [sys.executable, "-c", script, str(childfile)],
                0.25 if not cancel else 10,
            )
        )
        if cancel:
            for _ in range(100):
                if childfile.exists():
                    break
                await asyncio.sleep(0.01)
            task.cancel()
        with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
            await task
        child = int(childfile.read_text())
        for _ in range(100):
            stat = Path(f"/proc/{child}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("descendant still running after cleanup")

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_failed_runs_have_independent_invalid_manifests(tmp_path, monkeypatch):
    import lira.diagnostics.camera_quality as mod

    monkeypatch.setattr(mod, "external_owners", lambda nodes, **kwargs: [{"pid": 9}])
    args = SimpleNamespace(
        output=str(tmp_path),
        command="capture",
        scene="fixed A4",
        distance_cm=30,
        full_page=True,
        lock=str(tmp_path / "lock"),
        device="/dev/video11",
        sensor="/dev/v4l-subdev2",
    )
    assert asyncio.run(capture(args)) == 1
    assert asyncio.run(capture(args)) == 1
    records = list(tmp_path.glob("sample-*/manifest.json"))
    assert len(records) == 2
    assert all(not json.loads(p.read_text())["valid"] for p in records)


def test_repeated_cancel_waits_for_restore_completion():
    from lira.diagnostics.camera_quality import finish_restore

    async def scenario():
        done = asyncio.Event()

        async def restore():
            await asyncio.sleep(0.1)
            done.set()

        task = asyncio.create_task(finish_restore(restore()))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        assert not task.done()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done.is_set()

    asyncio.run(scenario())


def test_ownership_permission_denied_reports_incomplete_coverage(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    pid = root / "99999"
    pid.mkdir(parents=True)
    original = Path.read_text

    def denied(self, *a, **kw):
        if self.name == "comm":
            raise PermissionError("denied")
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", denied)
    coverage = {}
    assert external_owners(["/dev/video11"], root, coverage) == []
    assert not coverage["complete"]
    assert coverage["inaccessible"][0]["check"] == "command"
