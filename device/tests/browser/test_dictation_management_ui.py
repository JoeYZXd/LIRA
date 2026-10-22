"""Real HTTP management writes, synthetic family/library, no fixture routes."""
import io
from pathlib import Path
import pytest
from PIL import Image
from ui_harness import login_console


@pytest.mark.parametrize('width',[390,1440])
def test_management_persists_edits_and_preserves_conflict(dev_server,page,tmp_path,width):
    from test_dictation_routes import attach
    s=attach(dev_server.runtime,tmp_path)
    page.set_viewport_size(dict(width=width,height=950))
    login_console(page,dev_server)
    page.goto(dev_server.base_url+'/dev/dictation')
    page.get_by_label('孩子',exact=True).select_option('1')
    page.get_by_label('语文课本',exact=True).select_option('b')
    page.get_by_label('已学到课次').fill('2')
    page.get_by_role('button',name='保存教材进度').click()
    page.wait_for_function("document.getElementById('notice').textContent==='操作已完成'")
    assert s.profiles.get(1)['progress']['lesson']==2
    page.get_by_label('每项播报次数').fill('3')
    page.get_by_role('button',name='保存流程新版本').click()
    page.wait_for_function("document.getElementById('config-version').textContent==='当前版本 2'")
    assert s.settings.get_v2()['parameters']['playback']['repeat_count']==3
    skill=page.get_by_label('教学规则')
    skill.fill('优先复习易错词，保留本课覆盖。')
    s.skills.save('其他家长已保存的新规则',expected_revision=1)
    page.get_by_role('button',name='保存 skill 新版本').click()
    page.wait_for_function("document.getElementById('notice').textContent.includes('编辑已保留')")
    assert skill.input_value()=='优先复习易错词，保留本课覆盖。'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert page.locator('#repeat').evaluate('(el)=>el.getBoundingClientRect().height')>=44
    out=Path('tmp/dictation-v2-work');out.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(out/f'UI-management-{width}.png'),full_page=True)
    page.once('dialog',lambda d:d.accept())
    page.reload()
    assert page.get_by_label('教学规则').input_value()=='其他家长已保存的新规则'


def test_photos_review_and_facts_are_real_service_roundtrip(dev_server,page,tmp_path):
    import asyncio
    from test_dictation_routes import attach
    s=attach(dev_server.runtime,tmp_path)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(1) as pool:
        result=pool.submit(lambda:asyncio.run(s.prepare_task(dict(scope_type='lesson',start=2,proposals={'2':dict(words=['尽头','依靠'])}),owner_id=1,session_id='browser-task',paired_device_id='tv',callback_registration_id='cb'))).result()
    tid=result['task']['task_id']
    login_console(page,dev_server);page.goto(dev_server.base_url+'/dev/dictation')
    page.get_by_label('孩子',exact=True).select_option('1')
    page.get_by_role('button',name=tid,exact=False).click()
    data=io.BytesIO();Image.new('RGB',(30,20)).save(data,format='PNG')
    page.locator('#photos').set_input_files(dict(name='paper.png',mimeType='image/png',buffer=data.getvalue()))
    page.wait_for_function("document.getElementById('review-status').textContent.includes('照片 1 张')")
    page.get_by_label('核对状态',exact=True).select_option('reviewed_with_errors')
    page.get_by_label('尽头',exact=True).check()
    page.get_by_label('错字：尽',exact=True).check()
    page.get_by_label('核对备注',exact=True).fill('前后鼻音需再辨别')
    page.get_by_role('button',name='保存核对结果').click()
    page.wait_for_function("document.getElementById('review-status').textContent.includes('reviewed_with_errors')")
    assert s.results.get(1,tid)['errors'][0]['wrong_characters']==['尽']
    assert len(s.results.photos(1,tid))==1
    page.get_by_label('单元',exact=True).fill('1')
    page.get_by_label('课次',exact=True).fill('2')
    page.get_by_role('button',name='读取孩子本课知识记录').click()
    page.wait_for_function("document.getElementById('facts').textContent.includes('前后鼻音需再辨别')")

def _prepare_browser_task(service, session):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(service.prepare_task(
            dict(scope_type='lesson', start=2, proposals={'2': dict(words=['尽头', '依靠'])}),
            owner_id=1, session_id=session, paired_device_id='tv',
            callback_registration_id='cb'))).result()['task']


def test_task_navigation_discards_only_confirmed_review(dev_server, page, tmp_path):
    from test_dictation_routes import attach
    s = attach(dev_server.runtime, tmp_path)
    a, b = [_prepare_browser_task(s, name) for name in ('task-a', 'task-b')]
    login_console(page, dev_server)
    page.goto(dev_server.base_url + '/dev/dictation')
    page.locator('#member').select_option('1')
    page.get_by_role('button', name=a['task_id'], exact=False).click()
    page.locator('#review-note').fill('A draft')
    page.get_by_label('尽头', exact=True).check()
    page.locator('#refresh-detail').click()
    assert page.locator('#review-note').input_value() == 'A draft'
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.get_by_role('button', name=b['task_id'], exact=False).click()
    assert a['task_id'] in page.locator('#task-summary').inner_text()
    assert page.locator('#review-note').input_value() == 'A draft'
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name=b['task_id'], exact=False).click()
    page.wait_for_function('(id)=>document.getElementById("task-summary").textContent.includes(id)', arg=b['task_id'])
    assert page.locator('#review-note').input_value() == ''
    assert not page.get_by_label('尽头', exact=True).is_checked()
    page.get_by_role('button', name='保存核对结果').click()
    page.wait_for_function('document.getElementById("notice").textContent === "操作已完成"')
    assert s.results.get(1, b['task_id'])['note'] == ''
    assert s.results.get(1, b['task_id'])['errors'] == []
    page.locator('#review-note').fill('Member draft')
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.locator('#member').select_option('')
    assert page.locator('#member').input_value() == '1'
    assert page.locator('#review-note').input_value() == 'Member draft'
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('#member').select_option('')
    assert page.locator('#task-detail').is_hidden()
    page.locator('#member').select_option('1')
    page.get_by_role('button', name=b['task_id'], exact=False).click()
    from playwright.sync_api import expect
    expect(page.locator('#review-note')).to_have_value('')


def test_busy_save_locks_inputs_and_restores_disabled_controls(dev_server, page, tmp_path, monkeypatch):
    import threading
    from test_dictation_routes import attach
    s = attach(dev_server.runtime, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = s.settings.save_v2
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(s.settings, 'save_v2', delayed)
    login_console(page, dev_server)
    page.goto(dev_server.base_url + '/dev/dictation')
    page.locator('#repeat').fill('3')
    page.locator('#rate').evaluate('(el)=>el.disabled=true')
    page.get_by_role('button', name='保存流程新版本').click()
    try:
        assert entered.wait(3)
        assert page.locator('#repeat').is_disabled()
        assert page.locator('#member').is_disabled()
        assert page.locator('#skill').is_disabled()
    finally:
        release.set()
    page.wait_for_function('document.getElementById("notice").textContent === "操作已完成"')
    assert page.locator('#repeat').is_enabled()
    assert page.locator('#rate').is_disabled()
    assert s.settings.get_v2()['parameters']['playback']['repeat_count'] == 3


def test_task_summary_uses_repository_current_item(dev_server, page, tmp_path):
    from test_dictation_routes import attach
    s = attach(dev_server.runtime, tmp_path)
    task = _prepare_browser_task(s, 'current-item')
    phase = task['phases'][0]
    item = phase['items'][0]
    s.repository.receive(dict(schema_version=2, task_id=task['task_id'],
        content_hash=task['content_hash'], event_id='current-item', run_epoch=1,
        sequence=1, state_revision=1, command_id=None, phase_id=phase['phase_id'],
        item_id=item['item_id'], action='page_presented', state='presenting',
        actual_repeats=0, boot_id='boot', session_id='session', occurred_at='now'), paired_device_id='tv')
    login_console(page, dev_server)
    page.goto(dev_server.base_url + '/dev/dictation')
    page.locator('#member').select_option('1')
    page.get_by_role('button', name=task['task_id'], exact=False).click()
    from playwright.sync_api import expect
    expect(page.locator('#task-summary')).to_contain_text('当前项 ' + item['item_id'])

@pytest.mark.parametrize('accept', [False, True])
def test_prepared_task_auto_load_respects_review_draft(dev_server, page, tmp_path, monkeypatch, accept):
    from types import SimpleNamespace
    from test_dictation_routes import attach
    s = attach(dev_server.runtime, tmp_path)
    a, b = [_prepare_browser_task(s, name) for name in ('before-prepare', 'after-prepare')]
    class LocalAgent:
        def __init__(self, *args, **kwargs):
            self.messages = []
        async def respond(self, text):
            return dict(task=b, text='Prepared locally')
    monkeypatch.setattr('lira.ui.dictation.DictationAgent', LocalAgent)
    dev_server.runtime._llm = SimpleNamespace(_client=object(), _model='local-test')
    login_console(page, dev_server)
    page.goto(dev_server.base_url + '/dev/dictation')
    page.locator('#member').select_option('1')
    page.get_by_role('button', name=a['task_id'], exact=False).click()
    page.locator('#review-note').fill('Keep this review')
    page.locator('#request-text').fill('Prepare another task')
    page.once('dialog', lambda dialog: dialog.accept() if accept else dialog.dismiss())
    page.locator('#prepare-form button').first.click()
    page.wait_for_function('document.getElementById("notice").textContent === "操作已完成"')
    assert (b if accept else a)['task_id'] in page.locator('#task-summary').inner_text()
    assert page.locator('#review-note').input_value() == ('' if accept else 'Keep this review')

def test_manual_cloud_parameters_roundtrip(dev_server,page,tmp_path):
    from test_dictation_routes import attach
    from lira.dictation_cloud_tts import DEFAULT_TTS
    s=attach(dev_server.runtime,tmp_path)
    login_console(page,dev_server)
    page.goto(dev_server.base_url+'/dev/dictation')
    page.wait_for_function("document.getElementById('config-version').textContent==='当前版本 1'")
    page.locator('#manual-navigation').check()
    page.locator('#tts-provider').select_option('doubao')
    assert page.locator('#repeat').is_disabled()
    assert page.locator('#repeat').input_value()=='1'
    page.get_by_role('button',name='保存流程新版本').click()
    page.wait_for_function("document.getElementById('config-version').textContent==='当前版本 2'")
    config=s.settings.get_v2()['parameters']
    assert config['tts']==DEFAULT_TTS
    assert config['playback']['manual_navigation'] is True
    assert config['playback']['repeat_count']==1
    page.reload()
    assert page.locator('#manual-navigation').is_checked()
    assert page.get_by_label('豆包语速',exact=True).input_value()=='-20'
    assert page.get_by_label('语音指令',exact=True).input_value()==DEFAULT_TTS['instruction']

def test_backend_debug_switch_persists_without_touching_playback(dev_server,page,tmp_path):
    from test_dictation_routes import attach
    s=attach(dev_server.runtime,tmp_path)
    login_console(page,dev_server)
    page.goto(dev_server.base_url+'/dev/dictation')
    page.wait_for_function("document.getElementById('display-version').textContent==='当前版本 0'")
    assert not page.get_by_label('在电视上显示调试日志').is_checked()
    page.get_by_label('在电视上显示调试日志').check()
    page.get_by_role('button',name='保存电视调试设置').click()
    page.wait_for_function("document.getElementById('display-version').textContent.includes('当前版本 1')")
    assert s.settings.get_display()['debug_enabled'] is True
    assert s.settings.get_v2()['revision']==1
    page.reload()
    assert page.get_by_label('在电视上显示调试日志').is_checked()
    page.get_by_label('在电视上显示调试日志').uncheck()
    page.get_by_role('button',name='保存电视调试设置').click()
    page.wait_for_function("document.getElementById('display-version').textContent.includes('当前版本 2')")
    assert s.settings.get_display()['debug_enabled'] is False
