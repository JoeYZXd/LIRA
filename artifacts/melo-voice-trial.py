import time,wave
from lira_tts_trial import TtsEngine,to_pcm16
for backend in ('melo','kokoro'):
    tts=TtsEngine(backend=backend,num_threads=4)
    for name,text in [('weather','今天天气不错，最高二十度，最低十二度。中午可能会下雨，出门记得带把伞。'),
                      ('tones','重庆的银行今天营业。这个问题很重要，请重新检查一下。')]:
        start=time.monotonic(); a=tts.generate(text)
        print(backend,name,'SECONDS',round(time.monotonic()-start,2),'DURATION',round(len(a)/tts.sample_rate,2),flush=True)
        with wave.open('/tmp/'+backend+'-'+name+'.wav','wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(tts.sample_rate);f.writeframes(to_pcm16(a))
