"""Owned, bounded 24 kHz PCM playback on the paired television; never falls back."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import hashlib
import time
import uuid

import httpx


class TvAudioPlayer:
    def __init__(self, sender):
        if sender is None:
            raise ValueError('TV audio requires a paired receiver')
        self.sender = sender
        self.output_frames = 0
        self.receipts = deque(maxlen=32)
        self._lock = asyncio.Lock()
        self._active_id = None
        self._revision = 0

    async def open(self):
        status = await self.sender.probe()
        if 'pcm_audio_v1' not in (status.get('capabilities') or []):
            raise RuntimeError('TV receiver audio upgrade required')
        if not status.get('interactive') or not status.get('foreground'):
            raise RuntimeError('TV audio requires an open foreground receiver')

    async def close(self):
        await self.stop()

    async def _request(self, client, path, body):
        response = await client.post(self.sender.base + path, json=body,
            headers={'Authorization': 'Bearer ' + self.sender.token})
        response.raise_for_status()
        return response.json()

    async def stop(self):
        self._revision += 1
        audio_id = self._active_id
        if audio_id is not None:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                await self._request(client, '/audio/stop', {'id': audio_id})

    async def play_stream(self, chunks):
        revision = self._revision
        async with self._lock:
            if revision != self._revision:
                return
            audio_id = uuid.uuid4().hex
            self._active_id = audio_id
            receipt = {'id': audio_id, 'status': 'starting', 'pcm_bytes': 0}
            self.receipts.append(receipt)
            digest = hashlib.sha256()
            sequence = 0
            began = time.monotonic()
            completed = False
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                async def send(path, **body):
                    if revision != self._revision:
                        raise asyncio.CancelledError
                    # Only explicit queue-full rejection is safe to retry. Never
                    # replay a chunk after an ambiguous HTTP/network outcome.
                    while True:
                        if time.monotonic() - began > 130:
                            raise TimeoutError('TV audio exceeded playback limit')
                        try:
                            reply = await self._request(client, path, dict(id=audio_id, **body))
                            if reply.get('id') != audio_id:
                                raise RuntimeError('TV audio acknowledgement ID mismatch')
                            if reply.get('status') in ('failed', 'cancelled'):
                                raise RuntimeError('TV audio playback failed: ' + reply.get('error', ''))
                            return reply
                        except httpx.HTTPStatusError as exc:
                            reason = exc.response.json().get('error') if exc.response.status_code == 409 else None
                            safe_backpressure = reason == 'audio buffer full' or (path == '/audio/start' and reason == 'audio busy' and time.monotonic() - began < 2)
                            if not safe_backpressure:
                                raise
                            await asyncio.sleep(.05)
                            if revision != self._revision:
                                raise asyncio.CancelledError
                try:
                    await send('/audio/start', approved=True, sample_rate=24000, channels=1, format='s16le')
                    receipt['status'] = 'playing'
                    async for chunk in chunks:
                        if len(chunk) % 2:
                            raise ValueError('PCM must contain whole int16 samples')
                        for offset in range(0, len(chunk), 24000):
                            pcm = chunk[offset:offset + 24000]
                            if receipt['pcm_bytes'] + len(pcm) > 5760000:
                                raise ValueError('TV audio exceeds 120 seconds')
                            reply = await send('/audio/chunk', sequence=sequence,
                                pcm_base64=base64.b64encode(pcm).decode('ascii'))
                            if reply.get('next_sequence') != sequence + 1:
                                raise RuntimeError('TV audio sequence acknowledgement mismatch')
                            sequence += 1
                            receipt['pcm_bytes'] += len(pcm)
                            digest.update(pcm)
                    if not receipt['pcm_bytes']:
                        raise ValueError('Empty TV audio stream')
                    await send('/audio/end', total_bytes=receipt['pcm_bytes'])
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if revision != self._revision:
                            raise asyncio.CancelledError
                        response = await client.get(self.sender.base + '/status',
                            headers={'Authorization': 'Bearer ' + self.sender.token})
                        response.raise_for_status()
                        status = response.json()
                        audio = status.get('audio', {})
                        receipt['receiver'] = audio
                        if audio.get('id') != audio_id or not status.get('interactive') or not status.get('foreground'):
                            raise RuntimeError('TV audio ownership or foreground lost')
                        if audio.get('status') == 'completed':
                            frames = receipt['pcm_bytes'] // 2
                            if audio.get('written_frames') != frames or audio.get('played_frames', 0) < frames:
                                raise RuntimeError('TV playback drain not confirmed')
                            self.output_frames += frames
                            completed = True
                            receipt['status'] = 'completed'
                            return
                        if audio.get('status') in ('failed', 'cancelled'):
                            raise RuntimeError('TV audio playback failed: ' + audio.get('error', ''))
                        await asyncio.sleep(.05)
                    raise TimeoutError('TV audio drain timed out')
                except BaseException as exc:
                    receipt['status'] = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed'
                    receipt['error'] = str(exc)
                    raise
                finally:
                    receipt['pcm_sha256'] = digest.hexdigest()
                    if not completed:
                        # Stop is scoped to this ID; an old callback cannot stop
                        # a newer owner, and a failed POST never triggers replay.
                        try:
                            await asyncio.shield(self._request(client, '/audio/stop', {'id': audio_id}))
                        except (Exception, asyncio.CancelledError):
                            pass
                    if self._active_id == audio_id:
                        self._active_id = None
