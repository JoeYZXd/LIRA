"""Thumbnail -> linked lesson -> original page stays in the teaching card."""
import io
import re
import pytest
from PIL import Image
from playwright.sync_api import expect
from lira.textbook_import import TextbookImportStore
from ui_harness import login_console


@pytest.mark.parametrize('width',[390,1600])
@pytest.mark.parametrize('published',[False,True],ids=['draft','published-revision'])
def test_thumbnail_lesson_round_trip_preserves_origin_and_edits(dev_server,page,tmp_path,monkeypatch,width,published):
    monkeypatch.setenv('LIRA_TEXTBOOK_DIR',str(tmp_path/'books'))
    monkeypatch.setenv('LIRA_TEXTBOOK_IMPORT_DIR',str(tmp_path/'drafts'))
    store=TextbookImportStore(tmp_path/'drafts',tmp_path/'books')
    raw=io.BytesIO();Image.new('RGB',(300,400),'beige').save(raw,'PNG')
    draft=store.create([(str(n)+'.png',raw.getvalue()) for n in range(12)])
    rows=[dict(unit=1,number=1,title='第一课',four=['日'],two=['月'],source_pages=[1,9],reviewed=False),
          dict(unit=2,number=2,title='第二课',four=['山'],two=['水'],source_pages=[4,9],reviewed=False)]
    draft=store.update(draft['id'],dict(draft,title='缩略图核对课本',lessons=rows,page_units=[{'number':9,'unit':1}]))
    edited_id=draft['id'];original=None
    if published:
        for row in draft['lessons']:row['reviewed']=True
        draft=store.update(draft['id'],draft);store.publish(draft['id'],draft['revision'],True)
        original=(store.books_dir/(draft['id']+'.json')).read_bytes()
    page.set_viewport_size({'width':width,'height':1000});login_console(page,dev_server)
    page.click('#app-tile-teaching');hub=page.locator('#teaching-hub');url=page.url
    hub.get_by_role('button',name='语文',exact=False).click();hub.get_by_role('button',name='课本列表与导入',exact=False).click()
    frame=page.frame_locator('#teach-frame');frame.locator('#library .book-card').click()
    frame.get_by_role('tab',name='课本书页',exact=True).click()
    expect(frame.locator('#page-thumbnails button')).to_have_count(12)
    frame.get_by_role('button',name='查看第 9 页原页',exact=True).click()
    expect(frame.locator('#page-image')).to_have_attribute('alt','课本原始第 9 页')
    expect(frame.get_by_role('button',name='查看第 9 页原页',exact=True)).to_have_attribute('aria-current','page')
    expect(frame.locator('#page-linked-units')).to_have_text('第 1 单元 · 第 2 单元')
    frame.locator('#page-linked-lessons').get_by_role('button',name=re.compile('第 1 课')).click()
    expect(frame.get_by_label('课文标题',exact=True)).to_have_value('第一课')
    if published:
        frame.get_by_role('button',name='返回原书页 · 第 9 页',exact=True).click()
        frame.locator('#page-linked-lessons').get_by_role('button',name=re.compile('第 2 课')).click()
        frame.get_by_role('button',name='建立修订草稿，逐课核对',exact=True).click()
        frame.get_by_text('已建立修订草稿。原教材保持不变，请逐课核对后另存。',exact=True).wait_for()
        expect(frame.get_by_label('背诵内容（选填）',exact=True)).to_be_enabled()
        expect(frame.get_by_role('button',name='返回原书页 · 第 9 页',exact=True)).to_be_visible()
        expect(frame.get_by_label('课文标题',exact=True)).to_have_value('第二课')
        edited_id=next(item['id'] for item in store.list_drafts() if item['title'].endswith('（修订）'))
        frame.get_by_role('button',name='返回原书页 · 第 9 页',exact=True).click()
        frame.locator('#page-linked-lessons').get_by_role('button',name=re.compile('第 1 课')).click()
    frame.get_by_label('背诵内容（选填）',exact=True).fill('未保存的背诵内容')
    frame.get_by_role('button',name='返回原书页 · 第 9 页',exact=True).click()
    expect(frame.get_by_label('书页',exact=True)).to_have_value('9')
    frame.locator('#page-linked-lessons').get_by_role('button',name=re.compile('第 1 课')).click()
    expect(frame.get_by_label('背诵内容（选填）',exact=True)).to_have_value('未保存的背诵内容')
    frame.get_by_role('button',name='核对本课并保存草稿',exact=True).click()
    frame.get_by_text('本课已核对，当前草稿已保存。',exact=True).wait_for()
    frame.get_by_role('button',name='下一课',exact=True).click()
    expect(frame.get_by_label('课文标题',exact=True)).to_have_value('第二课')
    frame.get_by_role('tab',name='课本书页',exact=True).click()
    expect(frame.get_by_label('书页',exact=True)).to_have_value('9')
    expect(frame.locator('#page-image')).to_have_attribute('alt','课本原始第 9 页')
    assert store.get(edited_id)['lessons'][0]['text']=='未保存的背诵内容'
    if published:assert (store.books_dir/(draft['id']+'.json')).read_bytes()==original
    assert page.url==url
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    frame.get_by_role('button',name='查看第 12 页原页',exact=True).click()
    expect(frame.locator('#page-linked-lessons')).to_have_text('未关联课次')
    frame.get_by_role('button',name='查看第 12 页原页',exact=True).press('ArrowLeft')
    expect(frame.get_by_label('书页',exact=True)).to_have_value('11')
