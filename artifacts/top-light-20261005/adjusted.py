import asyncio,json,time
from pathlib import Path
import cv2,numpy as np
from lira.hal.board.camera_isp import V4l2IspCamera
from lira.vision.ocr_rknn import RknnOcrEngine
out=Path('/home/orangepi/lira/camera-experiments/top-light-adjusted-'+time.strftime('%Y%m%d-%H%M%S'));out.mkdir(parents=True)
class EvidenceCamera(V4l2IspCamera):
    async def _capture_batch(self,gain,n):
        frames=await super()._capture_batch(gain,n)
        self.frames=frames
        return frames
async def main():
    camera=EvidenceCamera(rotation=270);engine=RknnOcrEngine();report=[]
    try:
        await camera.open()
        for i,(exposure,gain) in enumerate([(6400,128),(3200,128),(3200,192)],1):
            await camera._sensor_set("exposure",exposure)
            camera._sensor_gain=gain
            camera._gain_limit=gain
            p=out/f'light-{i}.jpg';p.write_bytes(await camera.capture());image=cv2.imread(str(p));text='\n'.join(x.text for x in engine.read(image));p.with_suffix('.txt').write_text(text,encoding='utf-8')
            ys=[cv2.cvtColor(f,cv2.COLOR_BGR2GRAY).astype(np.float32) for f in camera.frames];differences=np.stack([(ys[j]-ys[j-1])/np.sqrt(2) for j in (1,2)])
            record={'round':i,'image':p.name,'gain':camera._sensor_gain,'exposure':exposure,'source_luma':float(ys[0].mean()),'random_luma_noise':float(differences.std()),'source_clipped_fraction':float(np.mean(camera.frames[0].max(axis=2)>=250)),'output_clipped_fraction':float(np.mean(image.max(axis=2)>=250)),'text':text};report.append(record);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');cv2.imwrite(str(out/f'raw-{i}.jpg'),camera.frames[0],[cv2.IMWRITE_JPEG_QUALITY,95]);print(json.dumps(record,ensure_ascii=False),flush=True)
    finally:await camera.close()
    print('RESULT_DIR='+str(out),flush=True)
asyncio.run(main())
