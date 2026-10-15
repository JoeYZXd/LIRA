import pytest
from lira.audio.tts_volcengine import encode_event, decode_event, ProtocolError

def test_binary_roundtrip_and_malformed():
    packet = encode_event(150, b'{}', 'session', server=True)
    assert decode_event(packet) == (150, 'session', b'{}')
    with pytest.raises(ProtocolError):
        decode_event(packet[:-1])

import asyncio
from types import SimpleNamespace
from lira.audio.tts_volcengine import VolcengineProvider
from lira.audio.tts_provider import OnlineTtsConfig

class Socket:
    def __init__(self, failure=False):
        self.messages = asyncio.Queue()
        self.closed = False
        self.session = ''
        self.failure = failure
        self.sent = []
    async def send(self, packet):
        event = int.from_bytes(packet[4:8], 'big')
        self.sent.append(event)
        if event in (100, 200):
            import json
            length = int.from_bytes(packet[8:12], 'big')
            body = json.loads(packet[16+length:])
            assert body['event'] == event
            assert body['req_params']['audio_params']['sample_rate'] == 22050
        if event == 1:
            await self.messages.put(encode_event(50, server=True))
        elif event == 100:
            size = int.from_bytes(packet[8:12], 'big')
            self.session = packet[12:12+size].decode()
            await self.messages.put(encode_event(150, session=self.session, server=True))
        elif event == 200:
            if self.failure:
                await self.messages.put(encode_event(153, session=self.session, server=True))
            else:
                await self.messages.put(encode_event(352, b'\x01', self.session, server=True))
                await self.messages.put(encode_event(352, b'\x02\x03\x04', self.session, server=True))
        elif event == 102:
            await self.messages.put(encode_event(152, session=self.session, server=True))
    async def recv(self):
        return await self.messages.get()
    async def close(self):
        self.closed = True

async def test_text_open_receives_pcm_and_odd_frames_join():
    ws = Socket()
    headers = {}
    async def connect(endpoint, **kwargs):
        headers.update(kwargs['additional_headers'])
        return ws
    provider = VolcengineProvider(OnlineTtsConfig(api_key='secret', speaker='voice'), connect=connect)
    release = asyncio.Event()
    async def text():
        yield '正文'
        await release.wait()
    stream = provider.stream(text(), sample_rate=22050, speed=1, allowed=lambda: True)
    assert await asyncio.wait_for(anext(stream), 1) == b'\x01\x02\x03\x04'
    assert 102 not in ws.sent
    release.set()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert ws.closed
    assert headers == {'X-Api-Key': 'secret', 'X-Api-Resource-Id': 'seed-tts-2.0'}

async def test_privacy_prevents_connection_and_timeout_closes():
    calls = []
    ws = Socket()
    async def connect(*args, **kwargs):
        calls.append(1)
        return ws
    provider = VolcengineProvider(OnlineTtsConfig(timeout_seconds=.02), connect=connect)
    async def text():
        await asyncio.Event().wait()
        yield ''
    stream = provider.stream(text(), sample_rate=22050, speed=1, allowed=lambda: False)
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    assert calls == []
    stream = provider.stream(text(), sample_rate=22050, speed=1, allowed=lambda: True)
    with pytest.raises(ProtocolError):
        await asyncio.wait_for(anext(stream), 1)
    assert ws.closed

@pytest.mark.parametrize('failure', [PermissionError('auth'), RuntimeError('429')])
async def test_connect_failures_are_terminal_once(failure):
    calls = []
    async def connect(*args, **kwargs):
        calls.append(1)
        raise failure
    async def text():
        yield '正文'
    provider = VolcengineProvider(OnlineTtsConfig(), connect=connect)
    with pytest.raises(ProtocolError, match='online TTS session failed'):
        await anext(provider.stream(text(), sample_rate=22050, speed=1, allowed=lambda: True))
    assert calls == [1]

@pytest.mark.parametrize('event,identity', [(150, 'stale'), (352, 'stale'), (150, None), (153, None), (50, None)])
async def test_bad_session_events_close_without_reconnect(event, identity):
    class BadSocket(Socket):
        async def send(self, packet):
            if int.from_bytes(packet[4:8], 'big') == 200:
                await self.messages.put(encode_event(event, session=self.session if identity is None else identity, server=True))
            else:
                await super().send(packet)
    ws = BadSocket()
    async def connect(*args, **kwargs): return ws
    async def text(): yield '正文'
    provider = VolcengineProvider(OnlineTtsConfig(), connect=connect)
    with pytest.raises(ProtocolError):
        await asyncio.wait_for(anext(provider.stream(text(), sample_rate=22050, speed=1, allowed=lambda: True)), 1)
    assert ws.closed
    assert ws.sent.count(1) == 1

def test_literal_server_audio_and_error_frames():
    # Independent wire fixture: v1, audio-only server/event, raw PCM, session 's'.
    packet = bytes.fromhex('11b40000000001600000000173000000020102')
    assert decode_event(packet) == (352, 's', b'\x01\x02')
    with pytest.raises(ProtocolError):
        decode_event(bytes.fromhex('11f0000000000191000000027b7d'))

async def test_sender_failure_closes_connection():
    ws = Socket()
    async def connect(*args, **kwargs): return ws
    async def text():
        raise RuntimeError('source failed')
        yield ''
    provider = VolcengineProvider(OnlineTtsConfig(), connect=connect)
    with pytest.raises(ProtocolError):
        await asyncio.wait_for(anext(provider.stream(text(), sample_rate=22050, speed=1, allowed=lambda: True)), 1)
    assert ws.closed
