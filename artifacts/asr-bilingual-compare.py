import os, time, wave
import numpy as np
from lira_asr_trial import StreamingAsr
for method in ('greedy_search','modified_beam_search'):
    os.environ['LIRA_ASR_DECODING_METHOD']=method
    asr=StreamingAsr()
    for lang in ('en','zh','mixed'):
        with wave.open('/tmp/lira-'+lang+'.wav') as f:
            sr=f.getframerate()
            x=np.frombuffer(f.readframes(f.getnframes()),np.int16).astype(np.float32)/32768
        x=np.interp(np.arange(int(len(x)*16000/sr))*sr/16000,np.arange(len(x)),x).astype(np.float32)
        s=asr.create_stream(); start=time.monotonic()
        for i in range(0,len(x),1600):
            s.feed(x[i:i+1600]); s.text()
        s.feed(np.zeros(32000,dtype=np.float32))
        print(method,lang,s.text(),'seconds',round(time.monotonic()-start,2),flush=True)
