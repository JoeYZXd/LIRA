import json
from types import SimpleNamespace

import pytest
from ui_harness import attach_voice_memory,login_console,wait_for_text


@pytest.mark.parametrize('width',[390,1440])
def test_real_voice_history_select_link_export_and_unlink(dev_server,page,width):
    from lira.voice_history import VoiceHistory
    rt=dev_server.runtime;m=attach_voice_memory(rt)
    member=m.people.create_member('测试成员')['id']
    h=VoiceHistory(rt.store)
    rt.conversation_paths=SimpleNamespace(history=h,snapshot=lambda:{'mode':'classic','state':'standby'})
    h.event('user_text','下次想继续聊种花 <img src=x onerror=alert(1)>')
    h.event('assistant_text','记得准备花盆');h.event('response_done')
    h.trace('local','voiceprint.progress',{'process':{'last_result':'声音连续，已累计'},'identity':{'status':'collecting'}},turn=1)
    h.trace('sent','session.update',{'session':{'instructions':'实际发送的上下文 <img src=x onerror=alert(1)>','token':'not-for-export'}},turn=1)
    h.trace('received','response.done',{'response':{'usage':{'tokens':42}}},turn=1);h.end()
    page.on('dialog',lambda dialog:dialog.accept())
    page.set_viewport_size({'width':width,'height':900})
    login_console(page,dev_server)
    page.click('#app-tile-voice-history')
    wait_for_text(page,'#vh-turns','继续聊种花')
    assert page.locator('#vh-turns img').count()==0
    page.get_by_text('用户明细 · 转写、声纹分数与识别过程',exact=True).click()
    wait_for_text(page,'#vh-turns','声音连续，已累计')
    page.get_by_text('小鹿明细 · 发送上下文、工具调用与豆包返回',exact=True).click()
    wait_for_text(page,'#vh-turns','实际发送的上下文')
    assert page.locator('#vh-turns img').count()==0
    assert 'not-for-export' not in page.locator('#vh-turns').inner_text()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.select_option('#vh-member',str(member));page.click('#vh-select-all');page.click('#vh-link')
    wait_for_text(page,'#vh-status','已关联')
    assert '种花' in m.recall(member)
    exported=page.request.get(dev_server.base_url+page.locator('#vh-export').get_attribute('href'))
    assert exported.status==200 and exported.json()['turns'][0]['assistant']=='记得准备花盆'
    assert str(exported.json()['links'][str(exported.json()['turns'][0]['id'])])==str(member)
    assert 'not-for-export' not in exported.text()
    page.select_option('#vh-member','unlink');page.click('#vh-select-all');page.click('#vh-link')
    wait_for_text(page,'#vh-status','关联已解除')
    assert not json.loads(m.recall(member))['manually_linked_conversations']
    rt.privacy._on=True;page.click('#vh-refresh')
    wait_for_text(page,'#vh-status','隐私')
    assert page.locator('#vh-turns').inner_text()==''
