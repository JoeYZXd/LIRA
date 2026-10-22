import pytest

from lira.realtime_audio_output import RealtimeAudioOutput


class Sink:
    output_frames = 0

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def open(self):
        self.calls.append('open')
        if self.fail:
            raise RuntimeError('offline')

    async def play_stream(self, chunks):
        self.calls.append('play')
        if self.fail:
            raise RuntimeError('offline')
        async for chunk in chunks:
            self.calls.append(chunk)

    async def stop(self):
        self.calls.append('stop')

    async def close(self):
        self.calls.append('close')


async def chunks():
    yield b'pcm'


async def test_routes_are_captured_before_scheduling_and_do_not_change_mid_stream():
    local, tv = Sink(), Sink()
    explicit = False
    player = RealtimeAudioOutput(local, tv, lambda: explicit)
    normal = player.play_stream(chunks())
    explicit = True
    exact = player.play_stream(chunks())
    explicit = False
    await normal
    await exact
    assert local.calls == ['play', b'pcm']
    assert tv.calls == ['play', b'pcm']
    await player.stop()
    await player.close()
    assert local.calls[-2:] == tv.calls[-2:] == ['stop', 'close']


async def test_offline_tv_does_not_block_normal_audio_or_fall_back_for_dictation():
    local, tv = Sink(), Sink(fail=True)
    explicit = False
    player = RealtimeAudioOutput(local, tv, lambda: explicit)
    await player.open()
    await player.play_stream(chunks())
    assert tv.calls == []
    explicit = True
    with pytest.raises(RuntimeError, match='offline'):
        await player.play_stream(chunks())
    assert local.calls == ['open', 'play', b'pcm']
