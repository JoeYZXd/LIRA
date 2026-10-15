import asyncio
import time
import sys
import importlib.util
from lira.hal.board import audio

if len(sys.argv) > 1:
    spec = importlib.util.spec_from_file_location("lira_audio_probe", sys.argv[1])
    audio = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = audio
    spec.loader.exec_module(audio)

started = time.monotonic()
def report(stage):
    print(f"{time.monotonic() - started:.3f}s {stage}", flush=True)

original = audio.sd.RawOutputStream
class ObservedStream:
    def __init__(self, **kwargs):
        report("stream_construct")
        self.stream = original(**kwargs)
    def __enter__(self):
        report("stream_enter_start")
        self.stream.__enter__()
        report("stream_enter_done")
        return self
    def __exit__(self, *args):
        report("stream_exit_start")
        self.stream.__exit__(*args)
        report("stream_exit_done")
    def write(self, data):
        return self.stream.write(data)
    def start(self):
        report("stream_start")
        self.stream.start()
    def close(self):
        report("stream_close_start")
        self.stream.close()
        report("stream_close_done")
    def abort(self):
        report("abort_start")
        self.stream.abort()
        report("abort_done")

audio.sd.RawOutputStream = ObservedStream
async def main():
    player = audio.SoundDevicePlayer(device="lira_audio", samplerate=22050)
    for index in range(15):
        report(f"iteration={index}")
        task = asyncio.create_task(player.play(b"\0\0" * 22050 * 5))
        await asyncio.sleep(0.3)
        report("stop_start")
        await player.stop()
        report("stop_done")
        await task
        report("play_done")
    report("natural_completion_start")
    await player.play(b"\0\0" * 4410)
    report("natural_completion_done")

asyncio.run(main())
