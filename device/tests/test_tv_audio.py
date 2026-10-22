import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from lira.tv_audio import TvAudioPlayer


@pytest.fixture
def transport(monkeypatch):
    calls = []
    state = {'status': 'idle'}
    async def probe():
        return {'interactive': True, 'foreground': True, 'capabilities': ['pcm_audio_v1']}
    sender = SimpleNamespace(base='http://192.168.3.25:40100', token='x'*64, probe=probe)
    def handler(request):
        assert request.headers['authorization'] == 'Bearer ' + sender.token
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        path = request.url.path
        if path == '/audio/start':
            state.update(id=body['id'], status='playing', next_sequence=0, written_frames=0, played_frames=0)
        elif path == '/audio/chunk':
            assert body['id'] == state['id'] and body['sequence'] == state['next_sequence']
            pcm = base64.b64decode(body['pcm_base64'])
            assert len(pcm) <= 24000
            state['written_frames'] += len(pcm)//2
            state['next_sequence'] += 1
        elif path == '/audio/end':
            assert body['total_bytes'] == state['written_frames']*2
            state.update(status='completed', played_frames=state['written_frames'])
        elif path == '/audio/stop':
            return httpx.Response(200, json={'stopped_id': body['id']})
        if path == '/status':
            return httpx.Response(200,json={'interactive':True,'foreground':True,'audio':dict(state)})
        return httpx.Response(200,json=dict(state))
    real = httpx.AsyncClient
    monkeypatch.setattr('lira.tv_audio.httpx.AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(handler),**kwargs))
    return TvAudioPlayer(sender), calls, state


async def chunks(*items):
    for item in items:
        yield item


async def test_pcm_split_and_verified_tv_drain(transport):
    player,calls,state=transport
    await player.open()
    await player.play_stream(chunks(b'12'*30000))
    assert player.output_frames == 30000
    assert player.receipts[-1]['status']=='completed'
    assert [len(base64.b64decode(body['pcm_base64'])) for path,body in calls if path=='/audio/chunk']==[24000,24000,12000]
    assert not any(path=='/audio/stop' for path,_ in calls)


async def test_written_but_not_played_is_failure(transport):
    player,calls,state=transport
    original=player._request
    async def request(client,path,body):
        reply=await original(client,path,body)
        if path=='/audio/end':state['played_frames']=0
        return reply
    player._request=request
    with pytest.raises(RuntimeError,match='drain not confirmed'):
        await player.play_stream(chunks(b'12'*10))
    assert player.output_frames==0 and calls[-1][0]=='/audio/stop'


async def test_cancel_mid_upload_stops_owned_stream(transport):
    player,calls,state=transport
    started=asyncio.Event()
    async def pending():
        yield b'12'*10
        started.set()
        await asyncio.Event().wait()
    task=asyncio.create_task(player.play_stream(pending()))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert calls[-1][0]=='/audio/stop'
    assert calls[-1][1]['id']==player.receipts[-1]['id']
    assert player.receipts[-1]['status']=='cancelled'


async def test_network_ambiguity_never_replays_chunk(transport):
    player,calls,state=transport
    original=player._request
    attempts=0
    async def request(client,path,body):
        nonlocal attempts
        if path=='/audio/chunk':
            attempts+=1
            raise httpx.ReadTimeout('ambiguous receipt')
        return await original(client,path,body)
    player._request=request
    with pytest.raises(httpx.ReadTimeout):await player.play_stream(chunks(b'12'*10))
    assert attempts==1 and calls[-1][0]=='/audio/stop'


@pytest.mark.parametrize('status',[
    {'interactive':True,'foreground':True,'capabilities':[]},
    {'interactive':False,'foreground':True,'capabilities':['pcm_audio_v1']},
    {'interactive':True,'foreground':False,'capabilities':['pcm_audio_v1']},
])
async def test_unavailable_receiver_never_outputs_or_falls_back(status):
    async def probe():return status
    player=TvAudioPlayer(SimpleNamespace(probe=probe))
    with pytest.raises(RuntimeError):await player.open()
    assert not player.receipts


async def test_queue_full_rejection_retries_without_duplicate_pcm(transport):
    player,calls,state=transport
    original=player._request;blocked=True
    async def request(client,path,body):
        nonlocal blocked
        if path=='/audio/chunk' and blocked:
            blocked=False
            response=httpx.Response(409,json={'error':'audio buffer full'},request=httpx.Request('POST','http://tv/audio/chunk'))
            response.raise_for_status()
        return await original(client,path,body)
    player._request=request
    await player.play_stream(chunks(b'12'*10))
    assert state['written_frames']==10


async def test_external_stop_prevents_next_chunk(transport):
    player,calls,state=transport
    async def pending():
        yield b'12'*10
        await player.stop()
        yield b'12'*10
    with pytest.raises(asyncio.CancelledError):await player.play_stream(pending())
    assert state['written_frames']==10
