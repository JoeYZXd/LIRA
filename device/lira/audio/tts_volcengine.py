"""Original V3 bidirectional adapter, based on Volcengine's official protocol.

No connection reuse/retry. Each live answer owns one bounded WebSocket session.
"""
from __future__ import annotations
import asyncio
import json
import struct
import uuid

ENDPOINT = 'wss://openspeech.bytedance.com/api/v3/tts/bidirection'
MAX_FRAME = 262144

class ProtocolError(RuntimeError):
    pass

def encode_event(event, payload=b'{}', session='', *, server=False):
    header = bytes((0x11, 0x94 if server else 0x14, 0x10, 0))
    packet = header + struct.pack('>I', event)
    if event >= 100 or (server and event in (50, 51, 52)):
        identity = session.encode()
        packet += struct.pack('>I', len(identity)) + identity
    return packet + struct.pack('>I', len(payload)) + payload

def decode_event(packet):
    try:
        if not isinstance(packet, bytes) or len(packet) > MAX_FRAME or len(packet) < 12:
            raise ValueError()
        if packet[0] != 0x11 or packet[1] not in (0x94, 0xb4) or packet[2] & 15:
            raise ValueError()
        event = struct.unpack_from('>I', packet, 4)[0]
        offset, session = 8, ''
        if event >= 100 or event in (50, 51, 52):
            size = struct.unpack_from('>I', packet, offset)[0]
            offset += 4
            session = packet[offset:offset+size].decode()
            offset += size
        size = struct.unpack_from('>I', packet, offset)[0]
        offset += 4
        if len(packet) != offset + size:
            raise ValueError()
        return event, session, packet[offset:]
    except (ValueError, struct.error, UnicodeError):
        raise ProtocolError('invalid TTS protocol frame') from None

class VolcengineProvider:
    def __init__(self, config, *, connect=None):
        self.config = config
        self._connect = connect

    async def stream(self, text, *, sample_rate, speed, allowed):
        def check():
            if not allowed():
                raise asyncio.CancelledError()
        async def bounded(operation):
            check()
            result = await asyncio.wait_for(operation(), self.config.timeout_seconds)
            check()
            return result
        check()
        connect = self._connect
        if connect is None:
            from websockets.asyncio.client import connect
        ws = None
        sender = None
        session = uuid.uuid4().hex
        async def send(event, payload=b'{}'):
            await bounded(lambda: ws.send(encode_event(event, payload, session if event >= 100 else '')))
        async def receive():
            return decode_event(await bounded(ws.recv))
        async def expect(expected):
            event, identity, payload = await receive()
            if event != expected or (event >= 100 and identity != session):
                raise ProtocolError('unexpected TTS lifecycle event')
        params = {'speaker': self.config.speaker, 'audio_params': {
            'format': 'pcm', 'sample_rate': sample_rate,
            'speech_rate': max(-50, min(100, round((speed - 1) * 100)))}}
        def body(event, extra=None):
            return json.dumps({'event': event, 'user': {'uid': 'lira'}, 'namespace': 'BidirectionalTTS',
                               'req_params': {**params, **(extra or {})}}).encode()
        try:
            ws = await bounded(lambda: connect(ENDPOINT, additional_headers={
                'X-Api-Key': self.config.api_key,
                'X-Api-Resource-Id': self.config.resource_id},
                max_size=MAX_FRAME, max_queue=1, write_limit=32768,
                open_timeout=self.config.timeout_seconds, close_timeout=1))
            await send(1)
            await expect(50)
            await send(100, body(100))
            await expect(150)
            async def transmit():
                async for delta in text:
                    if len(delta) > 256:
                        raise ProtocolError('TTS text fragment capacity exceeded')
                    await send(200, body(200, {'text': delta}))
                await send(102)
            sender = asyncio.create_task(transmit())
            trailing = b''
            ignored = 0
            while True:
                receiving = asyncio.create_task(receive())
                try:
                    done, _ = await asyncio.wait((receiving, sender), return_when=asyncio.FIRST_COMPLETED)
                    if sender in done:
                        await sender
                    event, identity, payload = await receiving
                finally:
                    receiving.cancel()
                    await asyncio.gather(receiving, return_exceptions=True)
                check()
                if identity != session:
                    raise ProtocolError('stale TTS session event')
                if event == 352:
                    pcm = trailing + payload
                    trailing = pcm[len(pcm)//2*2:]
                    pcm = pcm[:len(pcm)//2*2]
                    for offset in range(0, len(pcm), sample_rate * 4):
                        check()
                        yield pcm[offset:offset + sample_rate * 4]
                elif event == 152:
                    if not sender.done() or trailing:
                        raise ProtocolError('premature TTS completion')
                    await sender
                    await send(2)
                    return
                elif event in (350, 351):
                    ignored += 1
                    if ignored > 1024:
                        raise ProtocolError('excessive TTS metadata')
                else:
                    raise ProtocolError('TTS session failed')
        except asyncio.CancelledError:
            raise
        except Exception:
            # Provider errors can contain auth headers or echoed user content.
            raise ProtocolError('online TTS session failed') from None
        finally:
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if ws is not None:
                try:
                    await asyncio.wait_for(ws.close(), 1)
                except Exception:
                    pass
