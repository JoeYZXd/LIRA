"""Silent board probe: fixed text -> local TTS -> realtime audio -> saved reply.

No microphone input, speaker playback or real business-tool execution.
Run from device with the service running (credentials stay in process memory).
"""
import asyncio
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
TEXTS = ['你好小鹿，请用一句话介绍你自己。',
         '请告诉我设备现在的状态。', '帮我录入声纹。']


class SilentPlayer:
    def __init__(self): self.parts = []
    async def open(self): pass
    async def stop(self): pass
    async def close(self): pass
    async def play_stream(self, chunks):
        async for chunk in chunks:
            if sum(map(len,self.parts))+len(chunk)>24000*2*60:
                raise RuntimeError('reply audio limit exceeded')
            self.parts.append(chunk)


def service_environment():
    pid=subprocess.check_output(['systemctl','--user','show','lira','--property=MainPID','--value'],text=True).strip()
    for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in item:
            key,value=item.split(b'=',1)
            os.environ[key.decode()]=value.decode()
    from itsdangerous import URLSafeTimedSerializer
    db=sqlite3.connect('file:/home/orangepi/lira/data/device.db?mode=ro',uri=True)
    secret=db.execute('SELECT value FROM meta WHERE key=?',('dev_console_session_secret',)).fetchone()[0]
    db.close()
    cookie=URLSafeTimedSerializer(secret,salt='lira-dev-session').dumps('dev-session')
    headers={'Cookie':'lira_dev_session='+cookie,'Content-Type':'application/json'}
    def request(body=None):
        req=urllib.request.Request('http://127.0.0.1:8080/dev/api/conversation-path',headers=headers,
            data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req,timeout=15) as response:return json.load(response)
    state=request()
    if state['cloud_active'] or state['local_enrollment']:
        request({'action':'stop'})


async def run_case(tts, text, index, target):
    import numpy as np
    from websockets.asyncio.client import connect
    from lira.audio.doubao_realtime import RealtimeSession,RealtimeConfig,RealtimeError
    from lira.audio.resample import to_16khz
    generated_at=time.monotonic()
    samples=await asyncio.to_thread(tts.generate,text)
    audio=to_16khz(samples,tts.sample_rate)
    audio=np.pad(audio,(0,(-len(audio))%320))
    preparation=time.monotonic()-generated_at
    times={}
    events=[]
    tools=[]
    finished=asyncio.Event()
    sent_frames=0
    expected_frames=len(audio)//320
    class ObservedSocket:
        def __init__(self,ws): self.ws=ws
        async def send(self,data):
            nonlocal sent_frames
            await self.ws.send(data)
            event=json.loads(data)
            if event.get('type')=='input_audio_buffer.append':
                sent_frames+=1
                if sent_frames==expected_frames:times['input_end']=time.monotonic()
        async def recv(self):
            raw=await self.ws.recv()
            event=json.loads(raw)
            kind=event.get('type')
            if kind=='conversation.item.input_audio_transcription.completed':times.setdefault('asr_end',time.monotonic())
            if kind=='response.output_audio.delta':times.setdefault('first_audio',time.monotonic())
            if kind=='response.done':
                times['response_done']=time.monotonic()
                finished.set()
            return raw
        async def close(self):await self.ws.close()
    async def observed_connect(*args,**kwargs):return ObservedSocket(await connect(*args,**kwargs))
    def on_event(kind,value):
        if kind!='activity':events.append({'kind':kind,'text':value})
        if kind=='error':finished.set()
    async def tool(name,args):
        tools.append({'name':name,'arguments':args})
        if name=='get_device_status':
            return {'status':'ok','path':'doubao_realtime','engine':'standby','test_mode':True}
        return {'status':'simulated','reason':'文字转语音测试，只模拟工具，不实际录音或保存声纹。'}
    player=SilentPlayer()
    session=RealtimeSession(config=RealtimeConfig(
        api_key=os.environ['LIRA_DOUBAO_REALTIME_API_KEY'],voice=os.environ['LIRA_DOUBAO_REALTIME_VOICE']),
        player=player,on_event=on_event,on_tool=tool,connect=observed_connect)
    connect_at=time.monotonic()
    result={'case':index,'input_text':text,'input_audio_seconds':len(audio)/16000,
            'tts_preparation_seconds':preparation}
    feeder=None
    try:
        await session.open()
        result['connect_seconds']=time.monotonic()-connect_at
        async def feed():
            for offset in range(0,len(audio),1600):
                session.feed(audio[offset:offset+1600])
                await asyncio.sleep(.1)
            while not finished.is_set():
                session.feed(np.zeros(1600,np.float32))
                await asyncio.sleep(.1)
        feeder=asyncio.create_task(feed())
        await asyncio.wait_for(finished.wait(),45)
        if session.playback is not None:
            await asyncio.wait_for(asyncio.shield(session.playback),5)
        result['status']='failed' if session.failed else 'completed'
        for name,end in (('input_end_to_first_audio','first_audio'),('input_end_to_response_done','response_done')):
            result[name+'_seconds']=(times[end]-times['input_end'] if end in times and 'input_end' in times else None)
        result['asr_end_to_first_audio_seconds']=(times['first_audio']-times['asr_end']
            if 'first_audio' in times and 'asr_end' in times else None)
    except Exception as exc:
        result['status']='failed'
        result['error']=str(exc) if isinstance(exc,RealtimeError) else type(exc).__name__
    finally:
        if feeder is not None:
            feeder.cancel()
            await asyncio.gather(feeder,return_exceptions=True)
        await session.close()
    result.update(events=events,tools=tools,reply_audio_seconds=sum(map(len,player.parts))/48000)
    if player.parts:
        output=target/f'case-{index}-reply.wav'
        with wave.open(str(output),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(24000)
            stream.writeframes(b''.join(player.parts))
        result['reply_audio_path']=str(output)
    return result


async def main(texts):
    service_environment()
    from lira.audio.tts import TtsEngine
    target=Path.home()/'lira/data'/('doubao-text-audio-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    target.mkdir(mode=0o700)
    tts=await asyncio.to_thread(TtsEngine)
    rows=[]
    try:
        for index,text in enumerate(texts,1):
            row=await run_case(tts,text,index,target)
            rows.append(row)
            (target/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
            print(json.dumps(row,ensure_ascii=False),flush=True)
    finally:await tts.close()
    print('RESULT_DIRECTORY='+str(target),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text',action='append',help='自定义测试文字，可多次指定；默认运行三组固定语料。')
    args=parser.parse_args()
    texts=args.text or TEXTS
    if len(texts)>10 or any(not 1<=len(text.strip())<=200 for text in texts):
        parser.error('每次最多十组语料，每组需为 1～200 字。')
    asyncio.run(main(texts))
