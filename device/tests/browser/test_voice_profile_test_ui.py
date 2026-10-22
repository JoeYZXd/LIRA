from types import SimpleNamespace

import numpy as np
import pytest

from ui_harness import attach_voice_memory,login_console,wait_for_text


@pytest.mark.parametrize('width',[390,1440])
def test_single_utterance_profile_test_and_retry_preserve_profile(dev_server,page,width):
    from lira.personal_memory import SpeakerIdentity
    from lira.config import SpeakerConfig
    rt=dev_server.runtime;m=attach_voice_memory(rt)
    member=m.register_voice('测试用户','朋友',[1.,0.])
    revision=m.voice_revision(member)
    identity=SpeakerIdentity(m,None,extractor=object())
    identity.embedding=lambda samples:np.array([1.,0.],np.float32)
    rt.speaker_identity=identity;rt.voice_enrollment=SimpleNamespace(params=SpeakerConfig())
    async def stop():pass
    rt.conversation_paths=SimpleNamespace(stop=stop,snapshot=lambda:{'mode':'classic','state':'standby'})
    page.set_viewport_size({'width':width,'height':900})
    login_console(page,dev_server);page.click('#app-tile-voice-candidates')
    page.locator('#voice-candidates-view').get_by_role('button',name='测试',exact=True).click()
    page.click('#vt-start');page.wait_for_selector('#vt-stop:not([hidden])')
    rt.dev_handle.voice_profile_test.capture.feed(np.full(16000*4,.1,np.float32))
    page.click('#vt-stop');wait_for_text(page,'#vt-status','匹配成功')
    wait_for_text(page,'#vt-result','1.0000')
    page.get_by_role('button',name='是本人',exact=True).click()
    wait_for_text(page,'#vt-status','反馈已保存')
    assert m.voice_revision(member)==revision and not rt.dev_handle.dev_mutex.locked()
    identity.embedding=lambda samples:np.array([.7,np.sqrt(.51)],np.float32)
    page.click('#vt-start');page.wait_for_selector('#vt-stop:not([hidden])')
    rt.dev_handle.voice_profile_test.capture.feed(np.full(16000*4,.1,np.float32))
    page.click('#vt-stop');wait_for_text(page,'#vt-status','最高分未达到')
    wait_for_text(page,'#vt-result','0.7000')
    assert m.voice_revision(member)==revision
    page.click('#vt-start');page.wait_for_selector('#vt-stop:not([hidden])')
    page.get_by_role('button',name='关闭测试',exact=True).click()
    page.wait_for_function('!document.getElementById("voice-profile-test").open')
    page.click('#identity-match-details summary')
    wait_for_text(page,'#identity-match-records','用户确认')
    assert rt.guided_voice_capture is None and not rt.dev_handle.dev_mutex.locked()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
