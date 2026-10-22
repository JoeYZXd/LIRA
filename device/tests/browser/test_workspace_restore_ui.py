import pytest
from ui_harness import login_console,wait_for_text


@pytest.mark.parametrize('width',[390,1440])
def test_refresh_keeps_apps_order_expanded_details_and_log_tab(dev_server,page,width):
    page.set_viewport_size({'width':width,'height':900})
    login_console(page,dev_server)
    page.click('#app-tile-costs')
    page.click('#app-tile-voicelog')
    page.click('#app-tile-voice-history')
    page.click('#app-tile-jev')
    page.locator('#cost-details > summary').click()
    page.click('#tab-model')
    expected=page.locator('#workspace > .tile:not([hidden])').evaluate_all('nodes => nodes.map(n=>n.id)')
    page.reload()
    page.wait_for_function('document.getElementById("tab-model").getAttribute("aria-selected")==="true"')
    actual=page.locator('#workspace > .tile:not([hidden])').evaluate_all('nodes => nodes.map(n=>n.id)')
    assert actual==expected
    assert page.locator('#cost-details').get_attribute('open') is not None
    assert page.locator('#app-tile-jev').get_attribute('aria-pressed')=='false'
    assert page.locator('#app-tile-voice-history').get_attribute('aria-pressed')=='true'
    wait_for_text(page,'#vh-status','未装配')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_empty_workspace_and_invalid_saved_ids_survive_refresh(dev_server,page):
    login_console(page,dev_server)
    for app_id in page.locator('.app-launch[aria-pressed="true"]').evaluate_all('nodes=>nodes.map(n=>n.id)'):
        page.locator('#'+app_id).click()
    page.reload()
    wait_for_text(page,'#workspace-count','0 个')
    assert page.locator('#workspace-empty').is_visible()
    # Seed the next document; the current document correctly saves its own
    # layout during pagehide and would otherwise overwrite this fixture.
    page.add_init_script('''localStorage.setItem('lira.dev.workspace.v1',JSON.stringify({version:1,open:['removed-app','tile-settings'],order:['removed-app','tile-settings','tile-settings']}))''')
    page.reload()
    page.wait_for_function('document.querySelectorAll("#workspace > .tile:not([hidden])").length===1')
    assert page.locator('#tile-settings').is_visible()
    assert page.locator('#workspace > .tile').count()==13


def test_teaching_app_survives_textbook_return(dev_server,page):
    login_console(page,dev_server)
    page.click('#app-tile-teaching')
    original_url=page.url
    page.locator('#tile-teaching').get_by_role('button',name='语文',exact=False).click()
    page.locator('#tile-teaching').get_by_role('button',name='课本列表与导入',exact=False).click()
    frame=page.frame_locator('#teach-frame')
    assert frame.get_by_role('heading',name='我的课本',exact=True).is_visible()
    assert frame.get_by_role('link',name='返回控制台',exact=True).count()==0
    page.get_by_role('button',name='返回语文',exact=True).click()
    assert page.url==original_url
    assert page.locator('#tile-teaching').is_visible()
    assert page.locator('#app-tile-teaching').get_attribute('aria-pressed')=='true'
    assert page.locator('#teach-subject-title').inner_text()=='语文'
