import asyncio, importlib.util, json, time
from pathlib import Path
import sys
import cv2
spec=importlib.util.spec_from_file_location('lira.hal.board.nv12','/tmp/lira-overhead-nv12.py'); module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
spec=importlib.util.spec_from_file_location('candidate','/tmp/lira-overhead-camera-candidate.py'); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.__file__='/home/orangepi/lira/device/lira/hal/board/camera_isp.py'
async def main():
    out=Path('/home/orangepi/lira/camera-experiments/overhead-final-20261005');out.mkdir(exist_ok=True)
    camera=module.V4l2IspCamera(rotation=270)
    camera.calibration_path=Path("/tmp/lira-camera-color.json")
    try:
        await camera.open()
        for i in range(1,4):
            started=time.monotonic()
            data=await camera.capture(); p=out/f'test08-{i}.jpg'; p.write_bytes(data)
            from lira.vision.ocr_rknn import RknnOcrEngine
            engine=RknnOcrEngine()
            lines=engine.read(cv2.imread(str(p)))
            text='\n'.join(x.text for x in lines)
            (out/f'test08-{i}.txt').write_text(text,encoding='utf-8')
            print(json.dumps({'round':i,'text':text,'lines':len(lines),'seconds':round(time.monotonic()-started,2)},ensure_ascii=False),flush=True)
    finally: await camera.close()
asyncio.run(main())
