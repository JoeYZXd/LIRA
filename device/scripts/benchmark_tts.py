"""Board-only audible TTS benchmark. Temporarily stops and restores lira.service.

Run from device with its venv Python. No credentials or user utterances are saved.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEXTS = ["我在。", "好的，我们马上开始录入声纹，请跟着我说。",
         "请用平时说话的声音读出这句话。录制完成后，我会检查声音是否清楚，再告诉你下一步怎么做。"]


async def measure(settings, cloud=False):
    from lira.config import load_config
    from lira.audio.devices import board_audio_device
    from lira.audio.tts import TtsEngine
    from lira.audio.voice_metrics import VoiceMetrics, current_trace
    from lira.hal.board.audio import SoundDevicePlayer

    cfg = load_config(require_api_key=False)
    player = SoundDevicePlayer(device=board_audio_device(
        cfg.hal.speaker_device, direction="output", echo_cancel=cfg.hal.echo_cancel_enabled))
    started = time.monotonic()
    tts = TtsEngine(player=player)
    startup = time.monotonic() - started
    player.samplerate = tts.sample_rate
    await player.open()
    rows = []
    try:
        for index, text in enumerate(TEXTS):
            cached = await asyncio.to_thread(tts.generate, text, settings['tts_speed'])
            for mode in (['local', 'cached', 'cloud'] if cloud else ['local', 'cached']):
                for repeat in range(3):
                    metrics = VoiceMetrics(enabled=True)
                    token = current_trace.set(metrics.trace(utterance_id=len(rows)))
                    start = time.monotonic()
                    try:
                        if mode == 'cloud':
                            from lira.audio.tts_volcengine import VolcengineProvider
                            async def fragments():
                                yield text
                            # Independent provider path: no silent local fallback.
                            pcm = VolcengineProvider(cfg.online_tts).stream(
                                fragments(), sample_rate=24000, speed=settings['tts_speed'], allowed=lambda: True)
                            player.samplerate = 24000
                            async def scaled():
                                import numpy as np
                                async for chunk in pcm:
                                    samples = np.frombuffer(chunk, dtype='<i2').astype(np.float32)
                                    yield np.clip(samples * settings['volume'], -32768, 32767).astype('<i2').tobytes()
                            await asyncio.wait_for(player.play_stream(scaled()), 90)
                            status = 'completed'
                        else:
                            player.samplerate = tts.sample_rate
                            done = (tts.speak_cached(cached, volume=settings['volume']) if mode == 'cached'
                                    else tts.speak(text, speed=settings['tts_speed'], volume=settings['volume']))
                            await asyncio.wait_for(done.wait(), 90)
                            status = done.status
                    finally:
                        current_trace.reset(token)
                    events = metrics.snapshot()
                    first = next((e['at'] for e in events if e['event'] == 'output_consumed'), None)
                    drained = next((e['at'] for e in events if e['event'] == 'output_drained'), None)
                    summary = metrics.summary()
                    row = dict(mode=mode, text_index=index, repeat=repeat, status=status,
                               first_output_seconds=None if first is None else first-start,
                               complete_seconds=time.monotonic()-start,
                               playback_seconds=None if first is None or drained is None else drained-first,
                               metrics=summary, events=events, dropped_events=metrics.dropped_events)
                    rows.append(row)
                    print(json.dumps({k:v for k,v in row.items() if k not in ('events','metrics')}), flush=True)
                    await asyncio.sleep(.25)
    finally:
        await tts.close()
    return dict(model_startup_seconds=startup, sample_rate=tts.sample_rate,
                settings=settings, texts=TEXTS, rows=rows, cloud_tested=cloud)


def main():
    from itsdangerous import URLSafeTimedSerializer
    pid = subprocess.check_output(['systemctl','--user','show','lira','--property=MainPID','--value'], text=True).strip()
    for entry in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in entry:
            key, value = entry.split(b'=',1)
            os.environ[key.decode()] = value.decode()
    db = sqlite3.connect('file:/home/orangepi/lira/data/device.db?mode=ro', uri=True)
    secret = db.execute('SELECT value FROM meta WHERE key=?', ('dev_console_session_secret',)).fetchone()[0]
    db.close()
    cookie = URLSafeTimedSerializer(secret, salt='lira-dev-session').dumps('dev-session')
    req = urllib.request.Request('http://127.0.0.1:8080/dev/api/status', headers={'Cookie':'lira_dev_session='+cookie})
    with urllib.request.urlopen(req, timeout=10) as response:
        status = json.load(response)
    settings = {k:status[k] for k in ('volume','tts_speed')}
    cloud = '--cloud' in sys.argv
    if cloud:
        env_file = Path.home()/'.config/lira-tts-benchmark.env'
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, value = line.split('=',1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
        if not all(os.environ.get(k, '').strip() for k in ('LIRA_ONLINE_TTS_API_KEY','LIRA_ONLINE_TTS_SPEAKER')):
            raise SystemExit('Cloud benchmark credentials or speaker are missing')
        os.environ['LIRA_ONLINE_TTS_ENABLED'] = 'true'
    subprocess.run(['systemctl','--user','stop','lira'], check=True)
    try:
        result = asyncio.run(measure(settings, cloud))
        target = Path.home()/'lira/data'/('tts-benchmark-cloud.json' if cloud else 'tts-benchmark-local.json')
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print('RESULT_FILE='+str(target), flush=True)
    finally:
        subprocess.run(['systemctl','--user','start','lira'], check=True)


if __name__ == '__main__':
    main()
