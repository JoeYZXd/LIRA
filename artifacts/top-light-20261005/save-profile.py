import asyncio,json,shutil,subprocess,time
from pathlib import Path
import cv2
from lira.hal.board.camera_isp import V4l2IspCamera
from lira.vision.ocr_rknn import RknnOcrEngine
profile=Path('data/camera-color.json');backup=Path('/home/orangepi/lira/deploy-backups')/('top-light-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir();shutil.copy2(profile,backup/profile.name)
active=subprocess.run(['systemctl','--user','is-active','--quiet','lira']).returncode==0
subprocess.run(['systemctl','--user','stop','lira'],check=True)
async def main():
    shutil.copy2('/tmp/lira-top-light-profile.json',profile)
    camera=V4l2IspCamera(rotation=270)
    try:
        await camera.open();out=Path('/home/orangepi/lira/camera-experiments/top-light-production-20261005');out.mkdir(exist_ok=True);p=out/'production.jpg';p.write_bytes(await camera.capture());text='\n'.join(x.text for x in RknnOcrEngine().read(cv2.imread(str(p))));p.with_suffix('.txt').write_text(text,encoding='utf-8');print(json.dumps({'backup':str(backup),'gain':camera._sensor_gain,'text':text},ensure_ascii=False),flush=True)
    finally:await camera.close()
try:asyncio.run(main())
except BaseException:
    shutil.copy2(backup/profile.name,profile)
    raise
finally:
    if active:subprocess.run(['systemctl','--user','start','lira'],check=True)
