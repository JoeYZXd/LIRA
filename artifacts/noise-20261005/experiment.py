import asyncio,json,subprocess,time
from pathlib import Path
import cv2,numpy as np
from lira.diagnostics.camera_quality import build_parser,capture
from lira.hal.board.camera_isp import _apply_orientation,_merge_frames,_apply_color_gains,_digital_exposure
from lira.vision.ocr_rknn import RknnOcrEngine
root=Path('/home/orangepi/lira/camera-experiments/noise-20261005');root.mkdir(exist_ok=True)
profile=json.loads(Path('data/camera-color.json').read_text());gains=np.array(profile['gains_bgr'],np.float32)
engine=RknnOcrEngine();report=[]
async def main():
 for exposure,gain in [(3200,1984),(3200,1024),(6400,512),(6400,384),(6400,768),(6400,512)]:
  before=set(root.glob('sample-*'));args=build_parser().parse_args(['capture','--scene','overhead-noise-fixed','--page-description','current-overhead-height-unmeasured','--distance-cm','20','--width','4224','--height','3136','--gain',str(gain),'--exposure',str(exposure),'--vblank','3300','--frames','3','--warmup','5','--output',str(root)])
  if await capture(args)!=0:raise RuntimeError('capture failed')
  sample=(set(root.glob('sample-*'))-before).pop();frames=[_apply_orientation(cv2.imread(str(sample/f'frame-{i:03d}.png')),270,False) for i in range(3)]
  merged,aligned=_merge_frames(frames);image=_digital_exposure(_apply_color_gains(cv2.bilateralFilter(merged,9,50,50),gains));name=f'{len(report)+1:02d}-exp{exposure}-gain{gain}';p=root/(name+'.jpg');cv2.imwrite(str(p),image,[cv2.IMWRITE_JPEG_QUALITY,88]);lines=engine.read(cv2.imread(str(p)));text='\n'.join(x.text for x in lines);(root/(name+'.txt')).write_text(text,encoding='utf-8')
  # Temporal differences estimate random noise without treating printed texture as noise.
  ys=[cv2.cvtColor(f,cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames]
  differences=np.stack([(ys[i]-ys[i-1])/np.sqrt(2) for i in (1,2)])
  record={'name':name,'sample':str(sample),'exposure':exposure,'gain':gain,'aligned':aligned,'random_luma_noise':float(differences.std()),'source_luma':float(ys[0].mean()),'source_clipped_fraction':float(np.mean(frames[0].max(axis=2)>=250)),'text':text,'lines':len(lines)};report.append(record);(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(record,ensure_ascii=False),flush=True)
asyncio.run(main())
