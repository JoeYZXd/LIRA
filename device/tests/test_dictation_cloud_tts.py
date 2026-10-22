import base64,json
import httpx,pytest
from lira.dictation_cloud_tts import DEFAULT_TTS,synthesize_cloud

@pytest.mark.asyncio
async def test_cloud_exact_approved_parameters_and_complete_pcm():
    def reply(request):
        body=json.loads(request.content)['req_params'];assert body['text']=='山顶。'
        assert body['speaker']==DEFAULT_TTS['speaker'];assert body['audio_params']==dict(format='pcm',sample_rate=24000,speech_rate=-20,loudness_rate=30)
        assert json.loads(body['additions'])==dict(post_process=dict(pitch=0),context_texts=[DEFAULT_TTS['instruction']])
        assert request.headers['X-Api-Resource-Id']=='seed-tts-2.0'
        return httpx.Response(200,text='\n'.join(json.dumps(r) for r in [dict(code=0,data=base64.b64encode(b'\x01\x00').decode()),dict(code=0,data=base64.b64encode(b'\x02\x00').decode()),dict(code=20000000)]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        assert await synthesize_cloud('山顶',DEFAULT_TTS,'private-key',allowed=lambda:True,client=client)==b'\x01\x00\x02\x00'

@pytest.mark.asyncio
@pytest.mark.parametrize('rows',[[],[dict(code=0,data='AQA=')],[dict(code=20000000)],[dict(code=0,data='AQ=='),dict(code=20000000)],[dict(code=9,message='private-key')]])
async def test_cloud_rejects_incomplete_or_failed_audio(rows):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='\n'.join(map(json.dumps,rows))))) as client:
        with pytest.raises(ValueError) as error:await synthesize_cloud('山顶',DEFAULT_TTS,'private-key',allowed=lambda:True,client=client)
        assert 'private-key' not in str(error.value)

@pytest.mark.asyncio
async def test_cloud_privacy_blocks_network():
    def unexpected(req):raise AssertionError('must not send')
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError,match='撤销'):await synthesize_cloud('山顶',DEFAULT_TTS,'private-key',allowed=lambda:False,client=client)

@pytest.mark.asyncio
async def test_cloud_revocation_during_stream_discards_partial_audio():
    permitted=True
    class Stream(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            nonlocal permitted
            yield b'{"code":0,"data":"AQA="}\n'
            permitted=False
            yield b'{"code":20000000}\n'
        async def aclose(self):self.closed=True
    stream=Stream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=stream))) as client:
        with pytest.raises(ValueError,match='撤销'):await synthesize_cloud('山顶',DEFAULT_TTS,'private-key',allowed=lambda:permitted,client=client)
    assert stream.closed
