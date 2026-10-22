import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

from lira.audio.doubao_realtime import RealtimeConfig
from lira.privacy import PrivacyState
from lira.settings import DeviceSettings


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs=kwargs
        self.closed=False
        self.replied=False
    async def open(self): pass
    async def close(self): self.closed=True
    async def send(self,*args,**kwargs): pass
    def feed(self,samples):
        if np.any(samples) and not self.replied:
            self.replied=True
            async def reply():
                self.kwargs['on_event']('user_text','帮我录入声纹')
                result=await self.kwargs['on_tool']('start_voice_enrollment',{})
                assert result['status']=='simulated'
                async def chunks():yield b'\0'*960
                await self.kwargs['player'].play_stream(chunks())
                self.kwargs['on_event']('assistant_text','这是模拟，没有实际保存声纹。')
                self.kwargs['on_event']('response_done','')
            asyncio.create_task(reply())


def make_lab():
    from lira.ui.doubao_lab import DoubaoLab
    async def stop():pass
    rt=SimpleNamespace(privacy=PrivacyState(),settings=DeviceSettings(),
        tts=SimpleNamespace(generate=lambda text,speed:np.full(1600,.1,np.float32),sample_rate=16000),
        conversation_paths=SimpleNamespace(config=RealtimeConfig(api_key='never-export-key',voice='test'),
            stop=stop,external_test=False),_mic_paused=False)
    mutex=asyncio.Lock()
    return rt,DoubaoLab(rt,mutex,session_factory=FakeSession),mutex


async def wait_done(lab):
    for _ in range(100):
        if lab.turns and lab.turns[-1]['state'] in ('completed','failed','cancelled'):return
        await asyncio.sleep(.01)
    raise AssertionError('turn did not finish')


@pytest.mark.asyncio
async def test_multiturn_feedback_export_and_simulated_tools():
    rt,lab,mutex=make_lab()
    await lab.start()
    assert mutex.locked() and rt.conversation_paths.external_test
    await lab.submit('帮我录入声纹','request-1')
    await wait_done(lab)
    turn=lab.snapshot()['turns'][0]
    assert turn['transcript']=='帮我录入声纹'
    assert turn['tools'][0]['result']['status']=='simulated'
    assert turn['audio_available']
    lab.feedback(turn['id'],'tool','不要声称已经保存','应该说明是模拟')
    data=json.loads(lab.export('json'))
    assert data['turns'][0]['feedback']['note']=='不要声称已经保存'
    assert 'never-export-key' not in lab.export('json')
    assert '应该说明是模拟' in lab.export('markdown')
    assert lab.audio(turn['id']).startswith(b'RIFF')
    import io,zipfile
    with zipfile.ZipFile(io.BytesIO(lab.bundle())) as archive:
        assert 'conversation.json' in archive.namelist()
        assert archive.read(turn['id']+'.wav').startswith(b'RIFF')
    await lab.submit('重复提交不会多一轮','request-1')
    assert len(lab.turns)==1
    await lab.stop()
    assert not mutex.locked() and not rt.conversation_paths.external_test
    assert len(lab.snapshot()['turns'])==1


@pytest.mark.asyncio
async def test_privacy_clears_records_audio_and_releases_exclusive_input():
    rt,lab,mutex=make_lab()
    await lab.start()
    await lab.submit('测试','request-1')
    await wait_done(lab)
    await rt.privacy.set_enabled(True,source='test')
    assert not mutex.locked() and not lab.turns
    with pytest.raises(PermissionError):lab.export('json')


@pytest.mark.asyncio
async def test_invalid_input_and_conflicting_test_are_rejected():
    rt,lab,mutex=make_lab()
    await mutex.acquire()
    with pytest.raises(RuntimeError):await lab.start()
    mutex.release()
    await lab.start()
    with pytest.raises(ValueError):await lab.submit('x'*201,'request-1')
    with pytest.raises(ValueError):lab.feedback('unknown','tool','note','expected')
    await lab.close()


@pytest.mark.asyncio
async def test_idle_disconnect_is_visible_and_releases_device_without_losing_notes():
    rt,lab,mutex=make_lab()
    await lab.start()
    await lab.submit('测试','request-1')
    await wait_done(lab)
    turn=lab.turns[0]
    lab.feedback(turn['id'],'reply','保留这个问题','下次改进')
    lab._event('error','连接已中断')
    assert lab.state=='error' and lab.error=='连接已中断'
    for _ in range(150):
        if not mutex.locked():break
        await asyncio.sleep(.01)
    assert not mutex.locked() and not rt.conversation_paths.external_test
    assert lab.turns[0]['feedback']['note']=='保留这个问题'
    await lab.start()
    assert lab.state=='ready'
    await lab.close()


def test_authenticated_http_feedback_and_export():
    import time
    from test_dev_console import make_client,login
    from lira.ui.doubao_lab import DoubaoLab
    _,client,rt=make_client()
    template,_,_=make_lab()
    rt.tts,rt.conversation_paths,rt._mic_paused=template.tts,template.conversation_paths,False
    rt.dev_handle._doubao_lab=DoubaoLab(rt,rt.dev_handle.dev_mutex,session_factory=FakeSession)
    with client:
        assert client.get('/dev/api/doubao-lab/export').status_code==401
        login(client)
        assert client.post('/dev/api/doubao-lab',json={'action':'start'}).status_code==200
        r=client.post('/dev/api/doubao-lab',json={'action':'send','text':'测试','request_id':'r1'})
        assert r.status_code==200
        for _ in range(100):
            data=client.get('/dev/api/doubao-lab').json()
            if data['turns'][0]['state']=='completed':break
            time.sleep(.01)
        turn=data['turns'][0]
        assert turn['state']=='completed'
        assert client.get('/dev/api/doubao-lab/audio/'+turn['id']).content.startswith(b'RIFF')
        r=client.post('/dev/api/doubao-lab',json={'action':'feedback','turn_id':turn['id'],
            'category':{},'note':'test','expected':'test'})
        assert r.status_code==400
        result=client.get('/dev/api/doubao-lab/export?format=json')
        assert result.status_code==200 and result.json()['tool_execution']=='simulated'
        assert 'never-export-key' not in result.text
        assert client.get('/dev/api/doubao-lab/export?format=markdown').text.startswith('# LIRA')
        assert client.post('/dev/api/doubao-lab',json={'action':'stop'}).status_code==200
