import time
from lira_tts_trial import TtsEngine, speech_chunks
tts=TtsEngine(backend='kokoro',num_threads=4)
text='上海明天的天气以阴天为主，气温比较舒适。如果准备外出散步，建议带一把轻便的折叠伞，并在出门前再确认实时预报。老人可以选择平坦的路面，按照自己的身体情况安排活动时间。'
for name, part in [('whole',text),('first_chunk',next(speech_chunks(text)))]:
    start=time.monotonic(); a=tts.generate(part)
    print(name,'chars',len(part),'seconds',round(time.monotonic()-start,2),flush=True)
