import numpy as np
import pytest

from lira.diagnostics.camera_quality import (
    decode_nv12,
    parse_controls,
    parse_format,
    split_frames,
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
import sys
from pathlib import Path
from types import SimpleNamespace

from lira.diagnostics.camera_quality import (
    CameraSession,
    DeviceLock,
    capture,
    external_owners,
    run_process,
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
    with DeviceLock(tmp_path / "lock"), pytest.raises(RuntimeError):  # noqa: SIM117 - first lock must succeed before expected second failure
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


def test_production_registration_shift_direction_characterization():
    import cv2

    from lira.diagnostics.camera_quality import registration_evidence
    from lira.hal.board.camera_isp import _merge_frames

    ref = np.full((160, 512, 3), 255, np.uint8)
    cv2.putText(
        ref, "Ordinary body 123", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2
    )
    shifted = cv2.warpAffine(
        ref, np.float32([[1, 0, 1], [0, 1, 0]]), (512, 160), borderValue=(255, 255, 255)
    )
    merged, aligned = _merge_frames([ref, shifted])
    evidence = registration_evidence([ref, shifted], aligned)
    assert aligned
    assert evidence["shifts"][0]["production_applied_shift_pixels"][0] == pytest.approx(
        1, abs=0.01
    )
    # Correct inverse registration restores identical text, whereas current
    # production adds displacement. This is characterization, not a HAL change.
    corrected = cv2.warpAffine(shifted, np.float32([[1, 0, -1], [0, 1, 0]]), (512, 160))
    interior = np.s_[10:-10, 10:-10]
    assert np.array_equal(corrected[interior], ref[interior])
    assert np.abs(merged[interior].astype(float) - ref[interior]).mean() > 2
    far_shifted = cv2.warpAffine(
        ref,
        np.float32([[1, 0, 8], [0, 1, 0]]),
        (512, 160),
        borderValue=(255, 255, 255),
    )
    fallback, aligned = _merge_frames([ref, far_shifted])
    evidence = registration_evidence([ref, far_shifted], aligned)
    assert not aligned and evidence["method"] == "sharpest_frame_fallback"
    assert any(np.array_equal(fallback, frame) for frame in (ref, far_shifted))


def stage_fixture(tmp_path, count=3, rotation=0):
    import hashlib

    import cv2

    from lira.diagnostics.camera_quality import ROI_NAMES

    source = tmp_path / "capture"
    source.mkdir()
    rawfmt = (
        FMT.replace("4/4", "32/24").replace(": 6", ": 36").replace(": 40", ": 1296")
    )
    fmt = parse_format(rawfmt)
    entries = []
    for i in range(count):
        # Padded linear NV12; preserve input bytes including padding.
        raw = np.full((36, 36), 173, np.uint8)
        raw[:24, :32] = 90 + i * 7
        raw[5:17, 5:11] = 35 + i
        raw[24:, :32] = 128
        payload = raw.tobytes()
        rawname, pngname = f"frame-{i:03d}.nv12", f"frame-{i:03d}.png"
        (source / rawname).write_bytes(payload)
        cv2.imwrite(str(source / pngname), decode_nv12(payload, fmt))
        entries.append(
            {
                "raw": rawname,
                "png": pngname,
                "bytesused": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = {
        "schema": "lira.camera-quality/v1",
        "kind": "capture",
        "valid": True,
        "capture_ok": True,
        "restore": {"ok": True},
        "format": fmt,
        "format_raw": rawfmt,
        "scene": {"distance_cm": 30},
        "requested": {"frames": count, "rotation": rotation, "hflip": True},
        "frames": entries,
    }
    path = source / "manifest.json"
    path.write_text(json.dumps(manifest))
    roipath = tmp_path / "rois.json"
    roipath.write_text(json.dumps({name: [2, 2, 6, 7] for name in ROI_NAMES}))
    return SimpleNamespace(
        manifest=str(path), rois=str(roipath), output=str(tmp_path / "outputs")
    ), manifest


def test_offline_stages_same_batch_and_inputs_untouched(tmp_path, monkeypatch):
    import cv2

    import lira.diagnostics.camera_quality as mod
    from lira.hal.board.camera_isp import (
        _apply_orientation,
        _digital_exposure,
        _merge_frames,
    )

    args, source = stage_fixture(tmp_path, rotation=90)
    before = {p: p.read_bytes() for p in Path(args.manifest).parent.iterdir()}
    monkeypatch.setattr(
        mod, "external_owners", lambda *a, **kw: pytest.fail("camera access")
    )
    assert mod.analyze(args) == 0
    out = next(Path(args.output).glob("stages-*"))
    report = json.loads((out / "manifest.json").read_text())
    assert report["valid"] and len(report["sources"]) == 3
    assert len(report["outputs"]) == 7
    assert before == {p: p.read_bytes() for p in before}
    frames = [
        _apply_orientation(
            decode_nv12(
                before[Path(args.manifest).parent / e["raw"]], source["format"]
            ),
            90,
            True,
        )
        for e in source["frames"]
    ]
    merged, _ = _merge_frames(frames)
    bilateral = cv2.bilateralFilter(merged, 9, 50, 50)
    digital = _digital_exposure(bilateral)
    for stage, expected in [
        ("single-000", frames[0]),
        ("merged", merged),
        ("bilateral", bilateral),
        ("digital", digital),
    ]:
        actual = cv2.imread(str(out / (stage + ".png")))
        assert np.array_equal(actual, expected)
        assert np.array_equal(
            cv2.imread(str(out / (stage + "-center.png"))), expected[2:9, 2:8]
        )
    assert np.array_equal(
        cv2.imread(str(out / "jpeg88.jpg")), cv2.imread(str(out / "jpeg-decoded.png"))
    )
    assert mod.analyze(args) == 0
    assert len(list(Path(args.output).glob("stages-*"))) == 2


def test_single_frame_stages_report_single_source(tmp_path):
    from lira.diagnostics.camera_quality import analyze

    args, _ = stage_fixture(tmp_path, count=1)
    assert analyze(args) == 0
    report = json.loads(next(Path(args.output).glob("*/manifest.json")).read_text())
    assert report["registration"]["method"] == "single_frame"
    assert not report["registration"]["aligned_mean"]
    assert len(report["sources"]) == 1 and len(report["outputs"]) == 5


@pytest.mark.parametrize(
    "damage",
    ["hash", "missing", "png", "invalid", "count", "rois_empty", "rois_bounds"],
)
def test_offline_damaged_sources_and_rois_retain_failed_manifest(tmp_path, damage):
    from lira.diagnostics.camera_quality import analyze

    args, source = stage_fixture(tmp_path)
    root = Path(args.manifest).parent
    if damage == "hash":
        (root / source["frames"][0]["raw"]).write_bytes(b"wrong")
    elif damage == "missing":
        (root / source["frames"][1]["raw"]).unlink()
    elif damage == "png":
        (root / source["frames"][0]["png"]).write_bytes(b"corrupt")
    elif damage == "invalid":
        source["valid"] = False
    elif damage == "count":
        source["frames"].pop()
    elif damage == "rois_empty":
        Path(args.rois).write_text("{}")
    elif damage == "rois_bounds":
        rois = json.loads(Path(args.rois).read_text())
        rois["center"] = [30, 2, 8, 8]
        Path(args.rois).write_text(json.dumps(rois))
    Path(args.manifest).write_text(json.dumps(source))
    assert analyze(args) == 1
    report = json.loads(next(Path(args.output).glob("*/manifest.json")).read_text())
    assert not report["valid"] and report["error"] and report["outputs"] == []


def test_capture_parser_records_page_description_and_offline_parser():
    from lira.diagnostics.camera_quality import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "capture",
            "--scene",
            "book",
            "--distance-cm",
            "20",
            "--page-description",
            "open two-page book with pinyin",
        ]
    )
    assert args.page_description == "open two-page book with pinyin"
    offline = parser.parse_args(
        ["analyze", "--manifest", "sample/manifest.json", "--rois", "rois.json"]
    )
    assert offline.command == "analyze"
