import asyncio,json
from pathlib import Path
import cv2
from lira.hal.board.camera_isp import V4l2IspCamera
from lira.vision.ocr_rknn import RknnOcrEngine
async def main():
    camera=V4l2IspCamera(rotation=270);engine=RknnOcrEngine();out=Path('/home/orangepi/lira/camera-experiments/noise-production-20261005')
    try:
        await camera.open()
        for i in (2,3):
            p=out/f'production-{i}.jpg';p.write_bytes(await camera.capture());text='\n'.join(x.text for x in engine.read(cv2.imread(str(p))));p.with_suffix('.txt').write_text(text,encoding='utf-8');print(json.dumps({'round':i,'gain':camera._sensor_gain,'text':text},ensure_ascii=False),flush=True)
    finally:await camera.close()
asyncio.run(main())
