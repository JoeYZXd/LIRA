"""Controlled ISP NV12 capture. Run ``python -m lira.diagnostics.camera_quality --help``.

Only linear, single-plane NV12 is supported. v4l2-ctl verbose dqbuf bytesused
records define serialized frame boundaries; sizeimage is capacity, not payload.
"""

from __future__ import annotations
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import uuid

CONTROLS = ("vertical_blanking", "exposure", "analogue_gain")


def parse_format(raw):
    def field(pattern):
        m = re.search(pattern, raw, re.M)
        if not m:
            raise ValueError("Missing/ambiguous format field: " + pattern)
        return m

    w, h = map(int, field(r"Width/Height\s*:\s*(\d+)/(\d+)").groups())
    pixel = field(r"Pixel Format\s*:\s*'([^']+)'").group(1)
    planes = re.search(r"Number of planes\s*:\s*(\d+)", raw)
    stride = int(field(r"Bytes per Line\s*:\s*(\d+)").group(1))
    capacity = int(field(r"Size Image\s*:\s*(\d+)").group(1))
    if (
        pixel != "NV12"
        or (planes and int(planes.group(1)) != 1)
        or w % 2
        or h % 2
        or stride < w
        or stride % 2
    ):
        raise ValueError(
            "Only even, linear single-plane NV12 with explicit stride is supported"
        )
    if (
        len(re.findall(r"Bytes per Line\s*:", raw)) != 1
        or capacity < stride * h * 3 // 2
    ):
        raise ValueError("Ambiguous plane layout or undersized capacity")
    metadata = {
        k: v.strip()
        for k, v in re.findall(
            r"^\s*(Colorspace|Transfer Function|YCbCr/HSV Encoding|Quantization|Field)\s*:\s*(.+)$",
            raw,
            re.M,
        )
    }
    if metadata.get("Field", "None").lower() not in ("none", "any"):
        raise ValueError("Interlaced NV12 unsupported")
    return dict(
        width=w,
        height=h,
        pixelformat=pixel,
        planes=1,
        bytesperline=stride,
        sizeimage=capacity,
        color=metadata,
    )


def decode_nv12(payload, fmt):
    import cv2
    import numpy as np

    stride, h, w = fmt["bytesperline"], fmt["height"], fmt["width"]
    required = stride * h * 3 // 2
    if len(payload) != required:
        raise ValueError(
            f"NV12 payload {len(payload)} != explicit linear layout {required}"
        )
    yuv = (
        np.frombuffer(payload, dtype=np.uint8).reshape(h * 3 // 2, stride)[:, :w].copy()
    )
    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)


def split_frames(data, bytesused, count):
    if (
        len(bytesused) != count
        or any(n <= 0 for n in bytesused)
        or sum(bytesused) != len(data)
    ):
        raise ValueError(
            "Frame count/payload differs from verbose dqbuf bytesused records"
        )
    frames = []
    offset = 0
    for n in bytesused:
        frames.append(data[offset : offset + n])
        offset += n
    return frames


def parse_controls(raw):
    result = {}
    for line in raw.splitlines():
        m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+.*?:\s*(.*)", line)
        if m and m.group(1) in CONTROLS:
            vals = {
                k: int(v)
                for k, v in re.findall(
                    r"(min|max|step|default|value)=(-?\d+)", m.group(2)
                )
            }
            if not all(k in vals for k in ("min", "max", "step", "value")):
                raise ValueError("Unreadable control " + m.group(1))
            if vals["step"] <= 0 or vals["min"] > vals["max"]:
                raise ValueError("Invalid control range " + m.group(1))
            result[m.group(1)] = vals
    if set(result) != set(CONTROLS):
        raise ValueError("Exposure, gain and vblank ranges/readback required")
    return result


async def run_process(args, timeout=20):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode:
            raise RuntimeError(
                f"{args!r}: exit {proc.returncode}: {err.decode(errors='replace')} {out.decode(errors='replace')}"
            )
        return out.decode(errors="replace") + err.decode(errors="replace")
    finally:
        # The HAL launches children. Kill the session even if its leader exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


class DeviceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise RuntimeError("Another diagnostic capture owns the camera lock")
        return self

    def __exit__(self, *exc):
        self.file.close()


def external_owners(nodes, proc_root=Path("/proc"), coverage=None):
    """Check visible ownership; record inaccessible PID details without claiming
    complete system-wide coverage. Kernel streaming rejection is still mandatory.
    Known LIRA/3A controllers are rejected even when their fd table is hidden.
    """
    owners = []
    targets = {str(Path(n).resolve()) for n in nodes}
    unavailable = []
    for pid in proc_root.iterdir():
        if not pid.name.isdigit() or int(pid.name) == os.getpid():
            continue
        comm = ""
        cmd = ""
        try:
            comm = (pid / "comm").read_text().strip()
            cmd = (
                (pid / "cmdline")
                .read_bytes()
                .replace(b"\x00", b" ")
                .decode(errors="replace")
            )
        except FileNotFoundError:
            continue
        except PermissionError:
            unavailable.append({"pid": int(pid.name), "check": "command"})
        known = any(x in (comm + " " + cmd).lower() for x in ("rkaiq", "ispserver"))
        known = known or (comm.startswith("python") and "-m lira.main" in cmd)
        opened = []
        try:
            for fd in (pid / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                    if target in targets:
                        opened.append(target)
                except FileNotFoundError:
                    continue
                except PermissionError:
                    unavailable.append({"pid": int(pid.name), "check": "fd link"})
        except FileNotFoundError:
            continue
        except PermissionError:
            unavailable.append({"pid": int(pid.name), "check": "fd table"})
        if known or opened:
            owners.append(dict(pid=int(pid.name), command=cmd or comm, nodes=opened))
    if coverage is not None:
        coverage.update(
            {
                "method": "visible /proc commands and fd links",
                "complete": not unavailable,
                "inaccessible": unavailable,
                "limitation": "Hidden processes may own nodes; streaming must succeed and controls must remain stable. Operator must pause camera/3A services.",
            }
        )
    return owners


class CameraSession:
    def __init__(self, node, sensor, manifest, runner=run_process, timeout=20):
        self.node = node
        self.sensor = sensor
        self.m = manifest
        self.run = runner
        self.timeout = timeout

    async def ctl(self, node, *args):
        return await self.run(["v4l2-ctl", "-d", node, *args], self.timeout)

    async def controls(self):
        raw = await self.ctl(self.sensor, "--list-ctrls")
        self.m.setdefault("control_queries", []).append(raw)
        return parse_controls(raw)

    async def snapshot(self):
        self.original_raw = await self.ctl(self.node, "--get-fmt-video")
        self.original_format = parse_format(self.original_raw)
        self.original_controls = await self.controls()
        self.m["original"] = {
            "format": self.original_format,
            "format_raw": self.original_raw,
            "controls": self.original_controls,
        }

    async def set_control(self, name, value):
        await self.ctl(self.sensor, f"--set-ctrl={name}={value}")

    async def setup(self, args):
        for name, value in [
            ("vertical_blanking", args.vblank),
            ("exposure", args.exposure),
            ("analogue_gain", args.gain),
        ]:
            if value is None:
                continue
            ranges = await self.controls()
            c = ranges[name]
            if not c["min"] <= value <= c["max"] or (value - c["min"]) % c["step"]:
                raise ValueError(f"{name}={value} outside current range/step {c}")
            await self.set_control(name, value)
        self.m["readback"] = await self.controls()
        if args.width is not None:
            await self.ctl(
                self.node,
                f"--set-fmt-video=width={args.width},height={args.height},pixelformat=NV12",
            )
        raw = await self.ctl(self.node, "--get-fmt-video")
        self.m["format_raw"] = raw
        self.m["format"] = parse_format(raw)

    async def restore(self):
        errors = []
        # Reduce exposure before potentially reducing frame length; gain is independent.
        for action in (
            "lower_exposure",
            "format",
            "vblank",
            "exposure",
            "gain",
            "verify",
        ):
            try:
                old = self.original_controls
                fmt = self.original_format
                if action == "lower_exposure":
                    c = (await self.controls())["exposure"]
                    await self.set_control("exposure", c["min"])
                elif action == "format":
                    await self.ctl(
                        self.node,
                        f"--set-fmt-video=width={fmt['width']},height={fmt['height']},pixelformat=NV12",
                    )
                elif action == "vblank":
                    await self.set_control(
                        "vertical_blanking", old["vertical_blanking"]["value"]
                    )
                elif action == "exposure":
                    await self.set_control("exposure", old["exposure"]["value"])
                elif action == "gain":
                    await self.set_control(
                        "analogue_gain", old["analogue_gain"]["value"]
                    )
                else:
                    actual = await self.controls()
                    actual_fmt = parse_format(
                        await self.ctl(self.node, "--get-fmt-video")
                    )
                    self.m["restored_readback"] = actual
                    self.m["restored_format"] = actual_fmt
                    if actual_fmt != fmt or any(
                        actual[k]["value"] != old[k]["value"] for k in CONTROLS
                    ):
                        raise RuntimeError("Restore readback differs from snapshot")
            except Exception as e:
                errors.append(f"{action}: {e}")
        self.m["restore"] = {"ok": not errors, "errors": errors}


def write_json(path, obj):
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


async def finish_restore(coroutine):
    # Repeated SIGINT/cancel must not release ownership before restoration ends.
    task = asyncio.create_task(coroutine)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError()


async def capture(args):
    import cv2

    output = Path(args.output) / (
        "sample-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:10]
    )
    output.mkdir(parents=True, exist_ok=False)
    m = {
        "schema": "lira.camera-quality/v1",
        "kind": args.command,
        "valid": False,
        "capture_ok": False,
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scene": {
            "label": args.scene,
            "distance_cm": args.distance_cm,
            "page": "A4",
            "text": "ordinary body",
            "full_page_confirmed": args.full_page,
        },
        "requested": vars(args).copy(),
        "tool_version": 1,
    }
    m["requested"].pop("handler", None)
    try:
        with DeviceLock(args.lock):
            coverage = {}
            owners = external_owners([args.device, args.sensor], coverage=coverage)
            m["ownership_check"] = coverage
            if owners:
                raise RuntimeError(
                    f"Camera/3A externally owned; manually pause services: {owners}"
                )
            m["topology"] = await run_process(
                ["media-ctl", "-p", "-d", args.media], args.timeout
            )
            m["v4l2_version"] = await run_process(
                ["v4l2-ctl", "--version"], args.timeout
            )
            session = CameraSession(args.device, args.sensor, m, timeout=args.timeout)
            await session.snapshot()
            try:
                if args.command == "baseline":
                    if args.sensor != "/dev/v4l-subdev2":
                        raise ValueError(
                            "Production HAL hardcodes /dev/v4l-subdev2; baseline requires that sensor"
                        )
                    script = 'import asyncio,sys; from pathlib import Path; from lira.hal.board.camera_isp import V4l2IspCamera\nasync def main():\n c=V4l2IspCamera(sys.argv[1],int(sys.argv[3]),sys.argv[4]=="1")\n try:\n  await c.open(); Path(sys.argv[2]).write_bytes(await c.capture())\n finally: await c.close()\nasyncio.run(main())'
                    m["baseline_process"] = await run_process(
                        [
                            sys.executable,
                            "-c",
                            script,
                            args.device,
                            output / "baseline.jpg",
                            str(args.rotation),
                            "1" if args.hflip else "0",
                        ],
                        args.timeout,
                    )
                    m["readback"] = await session.controls()
                    m["format_raw"] = await session.ctl(args.device, "--get-fmt-video")
                    m["format"] = parse_format(m["format_raw"])
                    if cv2.imread(str(output / "baseline.jpg")) is None:
                        raise ValueError("Invalid baseline JPEG")
                    m["frames"] = [
                        {
                            "file": "baseline.jpg",
                            "sha256": hashlib.sha256(
                                (output / "baseline.jpg").read_bytes()
                            ).hexdigest(),
                        }
                    ]
                else:
                    await session.setup(args)
                    # Capture warmup and retained frames in ONE stream. All dqbuf records
                    # are serialized; discard warmup ourselves (no ambiguous stream-skip).
                    total = args.warmup + args.frames
                    rawpath = output / "stream.nv12"
                    log = await session.ctl(
                        args.device,
                        "--verbose",
                        "--stream-mmap",
                        f"--stream-count={total}",
                        f"--stream-to={rawpath}",
                    )
                    (output / "stream.log").write_text(log, encoding="utf-8")
                    sizes = [
                        int(x)
                        for x in re.findall(r"cap dqbuf:.*?bytesused:\s*(\d+)", log)
                    ]
                    payloads = split_frames(rawpath.read_bytes(), sizes, total)
                    after = parse_format(
                        await session.ctl(args.device, "--get-fmt-video")
                    )
                    if after != m["format"]:
                        raise ValueError("Format changed during streaming")
                    m["serialization"] = {
                        "method": "verbose cap dqbuf bytesused, all buffers written in order",
                        "bytesused": sizes,
                        "warmup_discarded": args.warmup,
                    }
                    m["frames"] = []
                    for i, payload in enumerate(payloads[args.warmup :]):
                        name = f"frame-{i:03d}"
                        (output / (name + ".nv12")).write_bytes(payload)
                        bgr = decode_nv12(payload, m["format"])
                        if not cv2.imwrite(str(output / (name + ".png")), bgr):
                            raise RuntimeError("PNG write failed")
                        m["frames"].append(
                            {
                                "raw": name + ".nv12",
                                "png": name + ".png",
                                "bytesused": len(payload),
                                "sha256": hashlib.sha256(payload).hexdigest(),
                            }
                        )
                    m["readback_after_capture"] = await session.controls()
                    if any(
                        m["readback"][k]["value"]
                        != m["readback_after_capture"][k]["value"]
                        for k in CONTROLS
                    ):
                        raise RuntimeError("Controls changed during capture")
                m["capture_ok"] = True
            finally:
                await finish_restore(session.restore())
            m["valid"] = m["capture_ok"] and m["restore"]["ok"]
    except BaseException as e:
        m["error"] = f"{type(e).__name__}: {e}"
    finally:
        write_json(output / "manifest.json", m)
    print(str(output))
    return 0 if m["valid"] else 1


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("capture", "baseline", "inspect"):
        q = sub.add_parser(name)
        q.add_argument("--device", default="/dev/video11")
        q.add_argument("--sensor", default="/dev/v4l-subdev2")
        q.add_argument("--media", default="/dev/media1")
        q.add_argument("--timeout", type=float, default=30)
        q.add_argument("--lock", default="/tmp/lira-camera-quality.lock")
        if name == "inspect":
            continue
        q.add_argument("--output", default="camera-experiments")
        q.add_argument("--scene", required=True)
        q.add_argument("--distance-cm", type=float, required=True)
        q.add_argument(
            "--full-page",
            action="store_true",
            help="Operator confirms complete A4 coverage; not inferred by tool",
        )
        q.add_argument("--exposure", type=int)
        q.add_argument("--gain", type=int)
        q.add_argument("--vblank", type=int)
        q.add_argument("--width", type=int)
        q.add_argument("--height", type=int)
        q.add_argument("--frames", type=int, default=3)
        q.add_argument("--warmup", type=int, default=5)
        q.add_argument("--rotation", type=int, choices=(0, 90, 180, 270), default=0)
        q.add_argument("--hflip", action="store_true")
    return p


async def dispatch(args):
    if args.command == "inspect":
        with DeviceLock(args.lock):
            coverage = {}
            owners = external_owners([args.device, args.sensor], coverage=coverage)
            if owners:
                raise RuntimeError(f"Externally owned: {owners}")
            s = CameraSession(
                args.device,
                args.sensor,
                {"ownership_check": coverage},
                timeout=args.timeout,
            )
            await s.snapshot()
            print(json.dumps(s.m, ensure_ascii=False, indent=2))
        return 0
    if (
        (args.width is None) != (args.height is None)
        or (args.width is not None and (args.width <= 0 or args.height <= 0))
        or args.frames < 1
        or args.warmup < 0
        or args.distance_cm <= 0
        or args.timeout <= 0
    ):
        raise ValueError("Invalid dimensions/frame count/distance/timeout")
    return await capture(args)


def main():
    args = build_parser().parse_args()
    try:
        return asyncio.run(dispatch(args))
    except (Exception, KeyboardInterrupt) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
