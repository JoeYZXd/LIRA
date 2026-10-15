import time
import wave
import numpy as np
from lira_tts_trial import TtsEngine, to_pcm16
from lira.audio.asr import StreamingAsr

tts = TtsEngine(backend='kokoro')
asr = StreamingAsr()
for name, text in [('zh', '你好，我是小丽拉。今天想聊些什么？'),
                   ('en', 'Hello, how are you today? What is the weather like in Shanghai?'),
                   ('mixed', '今天我们一起学习 English，明天再读一本书。')]:
    started = time.monotonic()
    samples = tts.generate(text)
    elapsed = time.monotonic() - started
    print(name, 'SYNTH_SECONDS', round(elapsed,2), 'RTF', round(elapsed/(len(samples)/tts.sample_rate),2), flush=True)
    with wave.open('/tmp/lira-'+name+'.wav', 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(tts.sample_rate)
        f.writeframes(to_pcm16(samples))
    resampled = np.interp(np.arange(int(len(samples)*16000/tts.sample_rate))*tts.sample_rate/16000,
                          np.arange(len(samples)), samples).astype(np.float32)
    s = asr.create_stream()
    for i in range(0,len(resampled),1600):
        s.feed(resampled[i:i+1600]); s.text()
    s.feed(np.zeros(32000,dtype=np.float32))
    print(name, 'ASR', s.text(), flush=True)
