"""Real authenticated console and embedded templates; synthetic library data."""
import pytest
import re
from ui_harness import login_console


@pytest.mark.parametrize('width',[390,1440])
def test_subject_navigation_embedded_edit_and_unsaved_guard(dev_server,page,width):
    page.set_viewport_size({'width':width,'height':900})
    page.route('**/dev/api/teaching',lambda r:r.fulfill(json={
        'model_available':False,'capabilities':[],'task':{'status':'idle'},
        'operations':[],'contents':{'contents':[]},'members':[]}))
    login_console(page,dev_server)
    page.click('#app-tile-teaching')
    original_url=page.url
    assert page.locator('#tile-teaching .tile-toggle').count()==0
    hub=page.locator('#teaching-hub')
    for subject,feature,text in [('数学','打印练习','打印机发现'),('英语','每日一个单词','不会自动安排')]:
        hub.get_by_role('button',name=subject,exact=False).click()
        hub.get_by_role('button',name=feature,exact=False).click()
        assert text in page.locator('#teach-planned').inner_text()
        assert page.locator('#teach-frame').is_hidden()
        hub.get_by_role('button',name='全部科目',exact=True).click()
    hub.get_by_role('button',name='语文',exact=False).click()
    hub.get_by_role('button',name=re.compile('^课文与诗词 ')).click()
    frame=page.frame_locator('#teach-frame')
    assert frame.get_by_label('完整原文').is_visible()
    assert frame.get_by_role('heading',name='能力与当前约定').is_hidden()
    frame.get_by_label('标题',exact=True).fill('尚未保存的原文')
    page.once('dialog',lambda dialog:dialog.dismiss())
    hub.get_by_role('button',name='返回语文',exact=True).click()
    assert frame.get_by_label('标题',exact=True).input_value()=='尚未保存的原文'
    page.once('dialog',lambda dialog:dialog.accept())
    hub.get_by_role('button',name='返回语文',exact=True).click()
    assert page.locator('#teach-subject-title').is_visible()
    hub.get_by_role('button',name='默写与听写',exact=False).click()
    assert frame.get_by_role('heading',name='语文默写管理',exact=True).is_visible()
    assert page.locator('#teach-frame').is_visible()
    assert page.url==original_url
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    hub.get_by_role('button',name='全部科目',exact=True).click()
    hub.locator('summary').click()
    hub.get_by_role('button',name='家长核对与修正',exact=True).click()
    assert frame.get_by_label('家长核对结果',exact=True).is_visible()
    assert frame.get_by_label('完整原文').is_hidden()
    assert page.url==original_url
