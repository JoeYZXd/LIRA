"""Validated linear single-plane NV12 layout and stream boundaries."""

import re

def parse_format(raw):
    def field(pattern):
        m = re.search(pattern, raw, re.MULTILINE)
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
            re.MULTILINE,
        )
    }
    if metadata.get("Field", "None").lower() not in ("none", "any"):
        raise ValueError("Interlaced NV12 unsupported")
    return {
        "width": w,
        "height": h,
        "pixelformat": pixel,
        "planes": 1,
        "bytesperline": stride,
        "sizeimage": capacity,
        "color": metadata,
    }


def decode_nv12(payload, fmt):
    import cv2
    import numpy as np

    stride, h, w = fmt["bytesperline"], fmt["height"], fmt["width"]
    required = stride * h * 3 // 2
    if len(payload) != required:
        raise ValueError(
            f"NV12 payload {len(payload)} != explicit linear layout {required}"
        )
    yuv = np.ascontiguousarray(
        np.frombuffer(payload, dtype=np.uint8).reshape(h * 3 // 2, stride)[:, :w]
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


