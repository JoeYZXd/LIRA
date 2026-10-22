import asyncio
import hashlib
import numpy as np
import pytest
from lira.dictation_assets import DictationAssets

class Engine:
    sample_rate=16000
    def generate(self,text):return np.ones(1600,dtype='float32')*.2

@pytest.mark.asyncio
async def test_silent_asset_generation_and_authenticated_chunks(tmp_path):
    assets=DictationAssets(tmp_path,Engine())
    task=dict(task_id='task',content_hash='a'*64)
    manifest,folder=await assets.prepare(task,{'prompt.prepare':'准备开始默写。','word':'依靠'})
    assert len(manifest['assets'])==2
    calls=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'status':'ok'}
    class Client:
        async def post(self,url,**kw):calls.append((url,kw));return Response()
    await assets.upload(manifest,folder,Client(),base='http://tv',token='paired-secret')
    assert all('/dictation/assets/' in url and kw['headers']['Authorization']=='Bearer paired-secret' for url,kw in calls)
    assert sum(url.endswith('/commit') for url,_ in calls)==2
    data=(folder/'word.pcm').read_bytes()
    assert manifest['assets'][1]['sha256']==hashlib.sha256(data).hexdigest()

@pytest.mark.asyncio
async def test_synthesis_failure_cleans_partial_and_never_uploads(tmp_path):
    class Broken(Engine):
        def generate(self,text):raise OSError('disk/model failed')
    assets=DictationAssets(tmp_path,Broken())
    with pytest.raises(OSError):await assets.prepare(dict(task_id='task',content_hash='a'*64),{'word':'依靠'})
    assert list(tmp_path.iterdir())==[]

@pytest.mark.asyncio
async def test_local_tamper_rejected_before_upload(tmp_path):
    assets=DictationAssets(tmp_path,Engine());manifest,folder=await assets.prepare(dict(task_id='task',content_hash='a'*64),{'word':'依靠'})
    (folder/'word.pcm').write_bytes(b'bad')
    with pytest.raises(ValueError):await assets.upload(manifest,folder,None,base='http://tv',token='secret')


def test_quiet_speech_is_audible_without_clipping():
    from lira.dictation_assets import pcm24
    source=(.01*np.sin(2*np.pi*440*np.arange(7802)/24000)).astype('float32')
    out=np.frombuffer(pcm24(source,24000),dtype='<i2').astype(float)/32767
    assert np.sqrt(np.mean(out*out)) >= .10
    assert abs(out).max() <= .90
    with pytest.raises(ValueError,match='silent'):
        pcm24(np.zeros(24000,dtype='float32'),24000)
