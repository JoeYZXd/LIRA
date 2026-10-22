from types import SimpleNamespace

import numpy as np
import pytest

from ui_harness import attach_voice_memory, login_console, wait_for_text


@pytest.mark.parametrize('width', [390, 1440])
def test_guided_enrollment_and_validation(dev_server, page, width):
    from lira.config import SpeakerConfig
    rt=dev_server.runtime
    memory=attach_voice_memory(rt)
    async def stop(): pass
    rt.conversation_paths=SimpleNamespace(stop=stop, snapshot=lambda: {'mode':'classic','state':'standby'})
    rt.voice_enrollment=SimpleNamespace(params=SpeakerConfig())
    rt.speaker_identity=SimpleNamespace(embedding=lambda a: np.array([1.,0.],np.float32))
    page.set_viewport_size({'width':width,'height':900})
    login_console(page,dev_server)
    page.click('#app-tile-voice-candidates')
    page.locator('#tile-voice-candidates').get_by_role('button',name='添加',exact=True).click()
    page.fill('#vg-name','引导测试')
    page.fill('#vg-address','朋友')
    page.click('#vg-begin')
    wait_for_text(page,'#vg-step','采集 1 / 4')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not memory.profiles()
    for i in range(5):
        page.click('#vg-record')
        page.wait_for_selector('#vg-stop:not([hidden])')
        # Feed the server capture consumer, never fabricate browser display state.
        rt.dev_handle.guided_voice.feed(np.ones(16000*9,np.float32)*.1)
        page.click('#vg-stop')
        if i<3: wait_for_text(page,'#vg-step',f'采集 {i+2} / 4')
        elif i==3: wait_for_text(page,'#vg-step','独立验证')
    wait_for_text(page,'#vg-message','独立验证通过')
    assert not memory.profiles()
    page.click('#vg-save')
    page.wait_for_function('!document.getElementById("voice-guide").open')
    assert len(memory.profiles())==1


def test_third_segment_rejection_remains_visible_after_status_poll(dev_server, page):
    from lira.config import SpeakerConfig
    rt=dev_server.runtime
    attach_voice_memory(rt)
    async def stop(): pass
    rt.conversation_paths=SimpleNamespace(stop=stop, snapshot=lambda: {'mode':'classic','state':'standby'})
    rt.voice_enrollment=SimpleNamespace(params=SpeakerConfig())
    rt.speaker_identity=SimpleNamespace(embedding=lambda a: np.array([1.,0.],np.float32))
    login_console(page,dev_server)
    page.click('#app-tile-voice-candidates')
    page.locator('#tile-voice-candidates').get_by_role('button',name='添加',exact=True).click()
    page.fill('#vg-name','失败提示测试')
    page.fill('#vg-address','朋友')
    page.click('#vg-begin')
    wait_for_text(page,'#vg-step','采集 1 / 4')
    for i in range(2):
        page.click('#vg-record')
        page.wait_for_selector('#vg-stop:not([hidden])')
        rt.dev_handle.guided_voice.feed(np.ones(16000*9,np.float32)*.1)
        page.click('#vg-stop')
        wait_for_text(page,'#vg-step',f'采集 {i+2} / 4')
    rt.speaker_identity.embedding=lambda a: np.array([.6,.8],np.float32)
    page.click('#vg-record')
    page.wait_for_selector('#vg-stop:not([hidden])')
    rt.dev_handle.guided_voice.feed(np.ones(16000*9,np.float32)*.1)
    page.click('#vg-stop')
    wait_for_text(page,'#vg-message','一致性')
    page.wait_for_timeout(2200)
    assert '一致性' in page.locator('#vg-message').inner_text()
    assert '采集 3 / 4' in page.locator('#vg-step').inner_text()
    assert len(rt.dev_handle.guided_voice.vectors)==2
    rt.speaker_identity.embedding=lambda a: np.array([.8,.6],np.float32)
    page.click('#vg-record')
    page.wait_for_selector('#vg-stop:not([hidden])')
    rt.dev_handle.guided_voice.feed(np.ones(16000*9,np.float32)*.1)
    page.click('#vg-stop')
    wait_for_text(page,'#vg-step','采集 4 / 4')
    wait_for_text(page,'#vg-message','临界区')
    page.wait_for_timeout(1200)
    assert '已保留' in page.locator('#vg-message').inner_text()
    page.on('dialog',lambda dialog:dialog.accept())
    page.get_by_role('button',name='放弃本次录入',exact=True).click()


def test_resolve_duplicate_replace_rerecord_cancel_and_delete(dev_server,page):
    from lira.config import SpeakerConfig
    rt=dev_server.runtime
    memory=attach_voice_memory(rt)
    member=memory.register_voice('原档案','旧称呼',[1.,0.])
    memory.people.append_messages(member,[('user','保留聊天')])
    async def stop(): pass
    rt.conversation_paths=SimpleNamespace(stop=stop,snapshot=lambda:{'mode':'classic','state':'standby'})
    rt.voice_enrollment=SimpleNamespace(params=SpeakerConfig())
    rt.speaker_identity=SimpleNamespace(embedding=lambda a: np.array([1.,0.],np.float32))
    page.on('dialog',lambda dialog:dialog.accept())
    page.set_viewport_size({'width':390,'height':900})
    login_console(page,dev_server)
    page.click('#app-tile-voice-candidates')
    page.locator('#tile-voice-candidates').get_by_role('button',name='添加',exact=True).click()
    page.fill('#vg-name','新姓名')
    page.fill('#vg-address','新称呼')
    page.click('#vg-begin')
    wait_for_text(page,'#vg-step','采集 1 / 4')
    for i in range(5):
        page.click('#vg-record')
        page.wait_for_selector('#vg-stop:not([hidden])')
        rt.dev_handle.guided_voice.feed(np.ones(16000*9,np.float32)*.1)
        page.click('#vg-stop')
        if i<3: wait_for_text(page,'#vg-step',f'采集 {i+2} / 4')
        elif i==3: wait_for_text(page,'#vg-step','独立验证')
    wait_for_text(page,'#vg-conflicts','声音相似')
    assert page.locator('#vg-save').is_hidden()
    page.select_option('#vg-target',str(member))
    page.get_by_role('button',name='确认修改并保存',exact=True).click()
    page.wait_for_function('!document.getElementById("voice-guide").open')
    assert len(memory.profiles())==1 and memory.people.get_member(member)['name']=='新姓名'
    page.get_by_role('button',name='重新录制',exact=True).click()
    assert page.locator('#vg-name').input_value()=='新姓名'
    page.click('#vg-begin')
    wait_for_text(page,'#vg-step','采集 1 / 4')
    revision=memory.voice_revision(member)
    page.get_by_role('button',name='暂时关闭（保留进度）',exact=True).click()
    page.wait_for_function('!document.getElementById("voice-guide").open')
    assert memory.voice_revision(member)==revision
    assert rt.dev_handle.guided_voice.token
    page.locator('#tile-voice-candidates').get_by_role('button',name='添加',exact=True).click()
    wait_for_text(page,'#vg-step','采集 1 / 4')
    page.get_by_role('button',name='放弃本次录入',exact=True).click()
    page.wait_for_function('!document.getElementById("voice-guide").open')
    page.get_by_role('button',name='删除声纹',exact=True).click()
    wait_for_text(page,'#voice-candidates-out','声纹已删除')
    assert not memory.profiles() and memory.people.get_member(member)
    assert memory.people.recent_messages(member)[0]['content']=='保留聊天'


def test_close_and_reload_restore_recordings_and_save_edited_name_once(dev_server,page):
    from lira.config import SpeakerConfig
    rt=dev_server.runtime
    memory=attach_voice_memory(rt)
    async def stop(): pass
    rt.conversation_paths=SimpleNamespace(stop=stop,snapshot=lambda:{'mode':'classic','state':'standby'})
    rt.voice_enrollment=SimpleNamespace(params=SpeakerConfig())
    rt.speaker_identity=SimpleNamespace(embedding=lambda a: np.array([1.,0.],np.float32))
    login_console(page,dev_server)
    page.click('#app-tile-voice-candidates')
    page.locator('#tile-voice-candidates').get_by_role('button',name='添加',exact=True).click()
    page.fill('#vg-name','原输入姓名')
    page.fill('#vg-address','朋友')
    page.click('#vg-begin')
    wait_for_text(page,'#vg-step','采集 1 / 4')
    for i in range(5):
        page.click('#vg-record')
        page.wait_for_selector('#vg-stop:not([hidden])')
        rt.dev_handle.guided_voice.feed(np.ones(16000*9,np.float32)*.1)
        page.click('#vg-stop')
        if i<3: wait_for_text(page,'#vg-step',f'采集 {i+2} / 4')
        elif i==3: wait_for_text(page,'#vg-step','独立验证')
    wait_for_text(page,'#vg-message','独立验证通过')
    token=rt.dev_handle.guided_voice.token
    page.get_by_role('button',name='暂时关闭（保留进度）',exact=True).click()
    page.wait_for_function('!document.getElementById("voice-guide").open')
    assert rt.dev_handle.guided_voice.token==token
    page.reload()
    page.wait_for_selector('#voice-guide[open]')
    wait_for_text(page,'#vg-message','独立验证通过')
    assert len(rt.dev_handle.guided_voice.vectors)==4 and not memory.profiles()
    page.fill('#vg-save-name','最终姓名')
    page.click('#vg-save')
    page.wait_for_function('!document.getElementById("voice-guide").open')
    assert memory.find_member('最终姓名') and not memory.find_member('原输入姓名')
