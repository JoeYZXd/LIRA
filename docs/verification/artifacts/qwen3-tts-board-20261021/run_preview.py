import os,time,json,resource,hashlib
from pathlib import Path
root=Path('/home/orangepi/lira/experiments/qwen3-tts');os.umask(0o077);out=root/'preview';out.mkdir(exist_ok=True)
import torch,numpy as np,soundfile as sf
from qwen_tts import Qwen3TTSModel
torch.set_num_threads(4);torch.set_num_interop_threads(1);torch.manual_seed(1234)
started=time.monotonic();print('LOADING CPU float32',flush=True)
model=Qwen3TTSModel.from_pretrained(str(root/'model'),device_map='cpu',dtype=torch.float32,attn_implementation='sdpa')
load=time.monotonic()-started;print(json.dumps(dict(loaded=True,load_seconds=load,peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)),flush=True);entries=[]
for name,text in [('shanding','山顶。'),('xingzhuang','形状。')]:
 started=time.monotonic()
 with torch.inference_mode():wavs,sr=model.generate_custom_voice(text=text,language='Chinese',speaker='Serena',max_new_tokens=96,do_sample=False,subtalker_dosample=False)
 seconds=time.monotonic()-started;x=np.asarray(wavs[0],dtype=np.float32);assert x.ndim==1 and len(x)>sr*.15 and np.isfinite(x).all();assert len(x)/sr<7.5,'generation reached duration guard'
 sf.write(str(out/(name+'-qwen3-original.wav')),x,sr,subtype='PCM_16')
 print(json.dumps(dict(word=text,stage='synthesized',seconds=seconds,audio_seconds=len(x)/sr),ensure_ascii=False),flush=True)
 # Pitch-preserving tempo control: changing WAV sample rate would lower tones.
 import librosa
 slow=librosa.effects.time_stretch(x,rate=.75);slow=np.concatenate([np.zeros(round(sr*.25),dtype='float32'),slow,np.zeros(round(sr*.5),dtype='float32')]);path=out/(name+'-qwen3-slow.wav');sf.write(str(path),slow,sr,subtype='PCM_16')
 entry=dict(word=text,model='Qwen3-TTS-12Hz-0.6B-CustomVoice',speaker='Serena',device='Orange Pi 5 CPU',dtype='float32',load_seconds=load,synthesis_seconds=seconds,raw_audio_seconds=len(x)/sr,real_time_factor=seconds/(len(x)/sr),sample_rate=sr,tempo_rate=.75,peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,sha256=hashlib.sha256(path.read_bytes()).hexdigest());entries.append(entry);(out/'report.json').write_text(json.dumps(entries,ensure_ascii=False,indent=2));print(json.dumps(entry,ensure_ascii=False),flush=True)
print('QWEN_CPU_PREVIEW_COMPLETED',flush=True)
