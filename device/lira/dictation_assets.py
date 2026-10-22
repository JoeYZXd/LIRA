"""Silent, bounded task PCM production and authenticated upload (never playback)."""
import asyncio
import hashlib
import re
from pathlib import Path
import numpy as np

MAX_ASSET_BYTES=24000*2*120
MAX_TASK_BYTES=64*1024*1024

def pcm24(samples,sample_rate):
    data=np.asarray(samples)
    if data.ndim!=1 or not data.size or data.size>192000*120 or not np.issubdtype(data.dtype,np.floating) or not np.isfinite(data).all():raise ValueError('invalid mono finite float samples')
    if type(sample_rate) is not int or not 8000<=sample_rate<=192000:raise ValueError('invalid sample rate')
    count=round(data.size*24000/sample_rate)
    if not 0<count<=MAX_ASSET_BYTES//2:raise ValueError('duration out of bounds')
    # Bandlimited conversion avoids aliasing when a model produces >24 kHz.
    if sample_rate==24000:converted=data
    else:
        # NumPy FIR fallback is also used by the existing input resampler, but
        # this task pipeline does not alter the classic audio implementation.
        grid=np.arange(127)-63
        cutoff=.45*min(24000,sample_rate)/sample_rate
        kernel=2*cutoff*np.sinc(2*cutoff*grid)*np.hamming(127)
        padded=np.pad(data,(63,63),mode='edge')
        filtered=np.convolve(padded,kernel/kernel.sum(),mode='valid')
        positions=np.arange(count)*sample_rate/24000
        converted=np.interp(positions,np.arange(len(data)),filtered)
    # Short local-TTS words can be ~-43 dBFS: a positive TV volume and a
    # drained AudioTrack then still do not imply audible speech. Normalize only
    # quiet task assets, with bounded gain and peak headroom; never boost noise.
    rms=float(np.sqrt(np.mean(np.asarray(converted,dtype=np.float64)**2)))
    peak=float(np.max(np.abs(converted)))
    if rms < 1e-5:raise ValueError('silent task audio')
    gain=min(32.0, .12/rms, .90/peak) if rms < .12 else 1.0
    return (np.clip(converted*gain,-1,1)*32767).round().astype('<i2').tobytes()

class DictationAssets:
    def __init__(self,root,engine):self.root=Path(root);self.engine=engine;self.lock=asyncio.Lock()
    async def prepare(self,task,texts):
        import shutil
        tid=task['task_id'];digest=task['content_hash']
        if not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',tid) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('invalid scope')
        if '..' in tid or type(texts) is not dict or not 1<=len(texts)<=1000 or any(type(t) is not str or not 0<len(t)<=16000 for t in texts.values()):raise ValueError('invalid synthesis manifest')
        folder=self.root/(tid+'-'+digest);manifest=[];total=0
        async with self.lock:
            self.root.mkdir(parents=True,exist_ok=True)
            # Bounded cache: caller retains current manifest, at most four task folders.
            old=sorted((p for p in self.root.iterdir() if p.is_dir() and not p.is_symlink()),key=lambda p:p.stat().st_mtime)
            for path in old[:-3]:shutil.rmtree(path)
            folder.mkdir(exist_ok=True)
            try:
                for aid,text in texts.items():
                    if not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',aid) or '..' in aid:raise ValueError('invalid asset id')
                    data=await asyncio.to_thread(self.engine.generate,text)
                    pcm=pcm24(data,self.engine.sample_rate);total+=len(pcm)
                    if total>MAX_TASK_BYTES:raise ValueError('task audio quota exceeded')
                    path=folder/(aid+'.pcm');temporary=path.with_suffix('.part')
                    temporary.write_bytes(pcm);temporary.replace(path)
                    manifest.append(dict(asset_id=aid,sha256=hashlib.sha256(pcm).hexdigest(),pcm_bytes=len(pcm),frames=len(pcm)//2,sample_rate=24000,channels=1))
            except BaseException:
                shutil.rmtree(folder,ignore_errors=True);raise
        return dict(task_id=tid,content_hash=digest,assets=manifest),folder

    async def upload(self,manifest,folder,client,*,base,token):
        import base64
        headers={'Authorization':'Bearer '+token}
        async def send(endpoint,payload):
            response=await client.post(base.rstrip('/')+'/dictation/assets/'+endpoint,json=payload,headers=headers)
            response.raise_for_status()
            payload=response.json()
            if payload.get('status')!='ok' and payload.get('ok') is not True:raise ValueError('asset upload rejected')
            return payload
        scope={k:manifest[k] for k in ('task_id','content_hash')}
        for asset in manifest['assets']:
            data=(Path(folder)/(asset['asset_id']+'.pcm')).read_bytes()
            if len(data)!=asset['pcm_bytes'] or hashlib.sha256(data).hexdigest()!=asset['sha256']:raise ValueError('local asset changed')
            started=await send('start',{**scope,**asset})
            if started.get('ready') is True:continue
            for offset in range(0,len(data),24000):
                await send('chunk',{**scope,'asset_id':asset['asset_id'],'offset':offset,'pcm_base64':base64.b64encode(data[offset:offset+24000]).decode('ascii')})
            await send('commit',{**scope,'asset_id':asset['asset_id']})
