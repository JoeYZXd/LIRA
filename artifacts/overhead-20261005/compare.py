from pathlib import Path
import cv2
import json
import numpy as np
from lira.hal.board.camera_isp import _apply_orientation, _digital_exposure, _merge_frames
from lira.vision.ocr_rknn import RknnOcrEngine
root=Path('/home/orangepi/lira/camera-experiments/sample-20261005-162813-89ca5d4004')
out=root/'comparison'; out.mkdir(exist_ok=True)
frames=[_apply_orientation(cv2.imread(str(root/f'frame-{i:03d}.png')),270,False) for i in range(3)]
merged,_=_merge_frames(frames); merged=cv2.bilateralFilter(merged,9,50,50)
# Experimental white-patch balance, assuming the brightest non-clipped patches are white.
small=cv2.resize(merged,(640,480)).astype(np.float32)
luma=small.mean(axis=2)
mask=(luma>=np.percentile(luma,95))&(small.max(axis=2)<250)&(small.min(axis=2)>80)
white=np.median(small[mask],axis=0)
gains=white.max()/white
neutral=np.clip(merged.astype(np.float32)*gains,0,255).astype(np.uint8)
e=RknnOcrEngine()
report=[]
for name,img in [('test02-fullwidth-current',_digital_exposure(merged)),('test03-fullwidth-native',merged),('test04-white-reference-assumption',neutral)]:
    cv2.imwrite(str(out/(name+'.jpg')),img,[cv2.IMWRITE_JPEG_QUALITY,95])
    # OCR consumes the exported image, so evidence matches exactly.
    decoded=cv2.imread(str(out/(name+'.jpg')))
    lines=e.read(decoded)
    text='\n'.join(x.text for x in lines)
    (out/(name+'.txt')).write_text(text,encoding='utf-8')
    report.append({'name':name,'shape':list(img.shape),'text':text,'lines':len(lines)})
(out/'report.json').write_text(json.dumps({'white_patch':white.tolist(),'gains':gains.tolist(),'results':report},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
