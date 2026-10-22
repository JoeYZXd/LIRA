"""Cover shelf and per-lesson persistence against the real authenticated API."""
import io
import re
import pytest
from PIL import Image
from playwright.sync_api import expect
from lira.textbook_import import TextbookImportStore
from ui_harness import login_console


def test_cached_pre_shelf_script_does_not_override_current_shelf(dev_server,page,tmp_path,monkeypatch):
    monkeypatch.setenv('LIRA_TEXTBOOK_DIR',str(tmp_path/'books'))
    monkeypatch.setenv('LIRA_TEXTBOOK_IMPORT_DIR',str(tmp_path/'drafts'))
    store=TextbookImportStore(tmp_path/'drafts',tmp_path/'books')
    raw=io.BytesIO();Image.new('RGB',(180,240),'beige').save(raw,'PNG')
    draft=store.create([('cover.png',raw.getvalue())])
    store.update(draft['id'],dict(draft,title='缓存验证课本',lessons=[]))
    # A webview/proxy may replay the pre-shelf script at its old stable URL.
    page.route('**/dev/static/textbooks.js',lambda r:r.fulfill(
        body="document.getElementById('library').textContent='缓存旧版行列表';window.legacyTextbookScript=true;",
        content_type='application/javascript'))
    login_console(page,dev_server);page.click('#app-tile-teaching')
    hub=page.locator('#teaching-hub');hub.get_by_role('button',name='语文',exact=False).click()
    hub.get_by_role('button',name='课本列表与导入',exact=False).click()
    frame=page.frame_locator('#teach-frame')
    expect(frame.locator('#library .book-card')).to_have_count(1,timeout=2000)
    assert '缓存旧版行列表' not in frame.locator('#library').inner_text()


@pytest.mark.parametrize('width',[390,1440])
def test_cover_fallback_lesson_review_and_published_revision(dev_server,page,tmp_path,monkeypatch,width):
    books=tmp_path/'books';drafts=tmp_path/'drafts'
    monkeypatch.setenv('LIRA_TEXTBOOK_DIR',str(books))
    monkeypatch.setenv('LIRA_TEXTBOOK_IMPORT_DIR',str(drafts))
    store=TextbookImportStore(drafts,books)
    image=io.BytesIO();Image.new('RGB',(180,240),'beige').save(image,'PNG')
    def create(title,cover=None):
        draft=store.create([('page.png',image.getvalue())])
        rows=[dict(number=1,unit=unit,title=title,four=['日'],two=['月'],source_pages=[1],reviewed=False) for unit,title in [(1,'春天'),(2,'秋天')]]
        return store.update(draft['id'],dict(draft,title=title,lessons=rows,cover_page=cover))
    draft=create('待核对语文',1)
    old=create('旧语文课本')
    for row in old['lessons']:row['reviewed']=True
    old=store.update(old['id'],old);store.publish(old['id'],old['revision'],True)
    original=(books/(old['id']+'.json')).read_bytes()
    page.set_viewport_size({'width':width,'height':1000});login_console(page,dev_server)
    page.click('#app-tile-teaching');hub=page.locator('#teaching-hub')
    hub.get_by_role('button',name='语文',exact=False).click()
    hub.get_by_role('button',name='课本列表与导入',exact=False).click()
    frame=page.frame_locator('#teach-frame');shelf=frame.locator('#library')
    cover=shelf.get_by_role('button',name=re.compile('待核对语文'))
    cover.locator('img').wait_for()
    assert cover.locator('img').evaluate('image=>image.complete&&image.naturalWidth>0')
    legacy=shelf.get_by_role('button',name=re.compile('旧语文课本'))
    assert legacy.locator('img').count()==0
    assert legacy.locator('.book-cover-name').inner_text()=='旧语文课本'
    cover.click()
    frame.get_by_role('tab',name='课文',exact=True).click()
    frame.locator('#lessons .lesson').wait_for()
    assert frame.locator('#lessons .lesson').count()==1
    assert frame.locator('#lesson-directory .lesson-unit').count()==2
    frame.get_by_label('扩词',exact=True).fill('日：日子、日光')
    frame.get_by_label('背诵内容（选填）',exact=True).fill('春天来了。')
    frame.get_by_role('button',name='下一课',exact=True).click()
    assert frame.get_by_label('课文标题',exact=True).input_value()=='秋天'
    frame.get_by_role('button',name='上一课',exact=True).click()
    assert frame.get_by_label('扩词',exact=True).input_value()=='日：日子、日光'
    frame.get_by_role('button',name='核对本课并保存草稿',exact=True).click()
    frame.get_by_text('本课已核对，当前草稿已保存。',exact=True).wait_for()
    saved=store.get(draft['id'])
    assert saved['lessons'][0]['reviewed'] is True and saved['lessons'][1]['reviewed'] is False
    assert saved['lessons'][0]['expansions']=='日：日子、日光'
    assert saved['lessons'][0]['text']=='春天来了。'
    assert frame.locator('#publish').is_disabled()
    frame.get_by_role('button',name='返回书架',exact=True).click()
    shelf.get_by_role('button',name=re.compile('旧语文课本')).click()
    frame.get_by_role('tab',name='课文',exact=True).click()
    assert frame.get_by_label('四会字',exact=True).is_disabled()
    frame.get_by_role('button',name='下一课',exact=True).click()
    assert frame.get_by_label('课文标题',exact=True).input_value()=='秋天'
    frame.get_by_role('button',name='建立修订草稿，逐课核对',exact=True).click()
    frame.get_by_text('已建立修订草稿。原教材保持不变，请逐课核对后另存。',exact=True).wait_for()
    expect(frame.get_by_label('四会字',exact=True)).to_be_enabled()
    assert (books/(old['id']+'.json')).read_bytes()==original
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
