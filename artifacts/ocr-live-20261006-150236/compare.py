import asyncio
import json
import time
import sys
from pathlib import Path

import cv2
import numpy as np

from lira.hal.board.camera_isp import V4l2IspCamera
from lira.vision.ocr_rknn import RknnOcrEngine

OUT = Path('/home/orangepi/lira/camera-experiments/ocr-live-20261006-150236')


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    camera = V4l2IspCamera(rotation=270)
    engine = RknnOcrEngine()
    report = []
    try:
        await camera.open()
        raw = Path(sys.argv[1]).read_bytes() if len(sys.argv) > 1 else await camera.capture()
        (OUT / 'capture.jpg').write_bytes(raw)
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        tiled = engine.detect
        for label, detect in [('before', engine._detect_single), ('after', tiled)]:
            engine.detect = detect
            started = time.monotonic()
            lines = engine.read(image)
            seconds = time.monotonic() - started
            text = '\n'.join(line.text for line in lines)
            (OUT / (label + '.txt')).write_text(text, encoding='utf-8')
            preview = image.copy()
            for i, line in enumerate(lines):
                points = np.int32(line.polygon)
                cv2.polylines(preview, [points], True, (0, 0, 255), 3)
                cv2.putText(preview, str(i+1), tuple(points[0]), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)
            cv2.imwrite(str(OUT / (label + '.jpg')), preview)
            record = {'mode': label, 'shape': list(image.shape), 'seconds': seconds, 'lines': len(lines), 'text': text,
                      'polygons': [np.asarray(line.polygon).tolist() for line in lines]}
            report.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
        (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        await camera.close()
        engine._det.release()
        engine._rec.release()


asyncio.run(main())

