import os,json,time,uuid,subprocess,base64,wave,hashlib
from pathlib import Path
import httpx
pid=subprocess.check_output(['systemctl','--user','show','lira','-p','MainPID','--value'],text=True).strip();env=dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
key=env.get('LIRA_ONLINE_TTS_API_KEY') or env.get('LIRA_DOUBAO_REALTIME_API_KEY');assert key,'No configured Doubao API key'
root=Path('/home/orangepi/lira/experiments/doubao-vivi2-preview');root.mkdir(parents=True,exist_ok=True,mode=0o700);os.umask(0o077)
settings=dict(speaker='zh_female_vv_uranus_bigtts',resource_id='seed-tts-2.0',pitch=0,speech_rate=-20,loudness_rate=30,context_texts=['朗读 播音 拉长音 着重强调 标准普通话'])
report=[]
with httpx.Client(timeout=40,trust_env=False) as client:
 for name,text in [('shanding','山顶。'),('xingzhuang','形状。')]:
  body=dict(user={'uid':'lira-preview'},req_params=dict(text=text,speaker=settings['speaker'],audio_params=dict(format='pcm',sample_rate=24000,speech_rate=-20,loudness_rate=30),additions=json.dumps(dict(post_process=dict(pitch=0),context_texts=settings['context_texts']),ensure_ascii=False)))
  started=time.monotonic();pcm=bytearray();terminal=False;first=None;codes=[]
  with client.stream('POST','https://openspeech.bytedance.com/api/v3/tts/unidirectional',headers={'X-Api-Key':key,'X-Api-Resource-Id':settings['resource_id'],'X-Api-Request-Id':uuid.uuid4().hex},json=body) as response:
   if response.status_code!=200:
    message=response.read().decode(errors='replace').replace(key,'<REDACTED>');print(json.dumps(dict(http_status=response.status_code,error=message[:500]),ensure_ascii=False),flush=True);raise SystemExit(2)
   for line in response.iter_lines():
    if not line:continue
    if line.startswith('data:'):line=line[5:].strip()
    chunk=json.loads(line);code=chunk.get('code');codes.append(code)
    if code not in (0,20000000):
     print(json.dumps(dict(error_code=code,message=str(chunk.get('message','')).replace(key,'<REDACTED>')[:400]),ensure_ascii=False),flush=True);raise SystemExit(3)
    if chunk.get('data'):
     if first is None:first=time.monotonic()-started
     pcm.extend(base64.b64decode(chunk['data'],validate=True))
     if len(pcm)>24000*2*30:raise ValueError('preview audio limit exceeded')
    if code==20000000:terminal=True
  assert terminal and pcm and len(pcm)%2==0,'audio incomplete'
  path=root/(name+'-vivi2.wav')
  with wave.open(str(path),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(pcm)
  entry=dict(word=text,settings=settings,request=body,api_seconds=round(time.monotonic()-started,3),first_audio_seconds=round(first,3),audio_seconds=len(pcm)/48000,pcm_bytes=len(pcm),terminal_success=terminal,sha256=hashlib.sha256(path.read_bytes()).hexdigest());report.append(entry);(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in entry.items() if k!='request'},ensure_ascii=False),flush=True)
print('VIVI2_PREVIEW_COMPLETED',flush=True)
