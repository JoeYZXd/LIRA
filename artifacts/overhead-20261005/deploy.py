import asyncio,json,shutil,subprocess,tarfile,time
from pathlib import Path
import cv2
from lira.appliances.store import ApplianceStore
root=Path('/home/orangepi/lira');backup=root/'deploy-backups'/('overhead-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(parents=True)
allowed=['device/lira/hal/board/camera_isp.py','device/lira/hal/board/nv12.py','device/lira/diagnostics/camera_quality.py','device/data/camera-color.json']
was_active=subprocess.run(['systemctl','--user','is-active','--quiet','lira']).returncode==0
store=ApplianceStore('data/device.db');old_rotation=store.get_meta('camera_rotation');changed=[]
with tarfile.open('/tmp/lira-overhead-deploy.tar.gz') as archive:
    assert sorted(m.name for m in archive.getmembers())==sorted(allowed)
    assert all(m.isfile() for m in archive.getmembers())
    subprocess.run(['systemctl','--user','stop','lira'],check=True)
    try:
        for name in allowed:
            target=root/name;dest=backup/name
            assert target.resolve().is_relative_to(root.resolve())
            existed=target.exists();changed.append((name,existed))
            if existed:dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,dest)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(archive.extractfile(name).read())
        store.set_meta('camera_rotation','270')
        (backup/'state.json').write_text(json.dumps({'rotation_before':old_rotation,'files':changed,'service_was_active':was_active},indent=2))
        from lira.hal.board.camera_isp import V4l2IspCamera
        async def capture():
            camera=V4l2IspCamera(rotation=270)
            try:
                await camera.open();image=await camera.capture()
                output=root/'camera-experiments'/'overhead-production-20261005';output.mkdir(exist_ok=True)
                p=output/'test09-production.jpg';p.write_bytes(image)
                from lira.vision.ocr_rknn import RknnOcrEngine
                engine=RknnOcrEngine();lines=engine.read(cv2.imread(str(p)))
                text='\n'.join(x.text for x in lines);(output/'test09-production.txt').write_text(text,encoding='utf-8')
                print(json.dumps({'backup':str(backup),'image':str(p),'ocr':text,'sensor_gain':camera._sensor_gain,'gains_bgr':camera.color_gains.tolist()},ensure_ascii=False),flush=True)
            finally:await camera.close()
        asyncio.run(capture())
    except BaseException:
        for name,existed in reversed(changed):
            target=root/name
            if existed:shutil.copy2(backup/name,target)
            else:target.unlink(missing_ok=True)
        store.set_meta('camera_rotation',old_rotation or '0')
        raise
    finally:
        if was_active:subprocess.run(['systemctl','--user','start','lira'],check=True)
