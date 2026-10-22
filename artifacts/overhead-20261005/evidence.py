from pathlib import Path
import cv2,numpy as np,json
from lira.vision.ocr_rknn import RknnOcrEngine
base=Path('/home/orangepi/lira/camera-experiments');out=base/'overhead-calibration-evidence';out.mkdir(exist_ok=True)
e=RknnOcrEngine()
for name,folder in [('calibration-gain512','sample-20261005-164519-23fe80d0be'),('calibration-gain384-exp3200','sample-20261005-164847-f301a0909b')]:
    raw=cv2.imread(str(base/folder/'frame-000.png'));image=np.ascontiguousarray(np.rot90(raw));p=out/(name+'.jpg');cv2.imwrite(str(p),image,[cv2.IMWRITE_JPEG_QUALITY,95]);lines=e.read(cv2.imread(str(p)));(out/(name+'.txt')).write_text('\n'.join(x.text for x in lines),encoding='utf-8')
    print(name,len(lines))
p=out/'test06-calibrated.jpg';p.write_bytes(Path('/tmp/lira-overhead-test06.jpg').read_bytes());lines=e.read(cv2.imread(str(p)));(out/'test06-calibrated.txt').write_text('\n'.join(x.text for x in lines),encoding='utf-8');print('test06',len(lines))
