"""Book sheets, page cover choice and associations use the real API."""
import io
import re
import pytest
from PIL import Image
from playwright.sync_api import expect
from lira.textbook_import import TextbookImportStore
from ui_harness import login_console


@pytest.mark.parametrize('width',[390,1600])
def test_book_sheets_page_links_and_published_cover(dev_server,page,tmp_path,monkeypatch,width):
    monkeypatch.setenv('LIRA_TEXTBOOK_DIR',str(tmp_path/'books'))
    monkeypatch.setenv('LIRA_TEXTBOOK_IMPORT_DIR',str(tmp_path/'drafts'))
    store=TextbookImportStore(tmp_path/'drafts',tmp_path/'books')
    out=io.BytesIO();Image.new('RGB',(180,240),'beige').save(out,'PNG')
    def make(title):
        d=store.create([(str(n)+'.png',out.getvalue()) for n in range(3)])
        rows=[dict(unit=i,number=i,title='课文'+str(i),four=['日'],two=['月'],source_pages=[],reviewed=True) for i in (1,2)]
        return store.update(d['id'],dict(d,title=title,lessons=rows))
    draft=make('待核对教材');published=make('已发布教材');book=store.publish(published['id'],published['revision'],True)
    original=(store.books_dir/(book['id']+'.json')).read_bytes()
    page.set_viewport_size({'width':width,'height':1000});login_console(page,dev_server)
    page.click('#app-tile-teaching');hub=page.locator('#teaching-hub')
    assert page.locator('#tile-teaching').bounding_box()['width']<=1080
    if width==1600:assert page.locator('#tile-teaching').bounding_box()['width']>=1079
    hub.get_by_role('button',name='语文',exact=False).click();hub.get_by_role('button',name='课本列表与导入',exact=False).click()
    frame=page.frame_locator('#teach-frame');shelf=frame.locator('#library')
    frame.get_by_role('button',name='导入课本',exact=True).click()
    frame.get_by_role('button',name='导入课本',exact=True).click()
    expect(frame.get_by_label('课本文件',exact=True)).to_be_visible()
    shelf.get_by_role('button',name=re.compile('已发布教材')).click()
    frame.get_by_role('tab',name='课本书页',exact=True).click()
    expect(frame.get_by_label('书页',exact=True)).to_have_value('1')
    frame.get_by_role('button',name='下一页',exact=True).click()
    frame.get_by_role('button',name='设为封面',exact=True).click()
    frame.get_by_text('封面已保存。',exact=True).wait_for()
    assert store.get(book['id'])['cover_page']==2
    assert (store.books_dir/(book['id']+'.json')).read_bytes()==original
    frame.get_by_role('tab',name='课本信息',exact=True).click()
    expect(frame.get_by_label('课本封面',exact=True)).to_have_value('2')
    frame.get_by_role('button',name='返回书架',exact=True).click()
    shelf.get_by_role('button',name=re.compile('待核对教材')).click()
    frame.get_by_role('tab',name='课本书页',exact=True).click()
    frame.get_by_label('书页',exact=True).select_option('2')
    frame.get_by_label('单元（选填）',exact=True).fill('2')
    frame.get_by_label('课次（可多选）',exact=True).select_option(['1'])
    frame.get_by_role('button',name='保存本页关联',exact=True).click()
    frame.get_by_text('本页关联已保存。',exact=True).wait_for()
    saved=store.get(draft['id'])
    assert saved['pages'][1]['unit']==2 and saved['lessons'][1]['source_pages']==[2]
    assert saved['lessons'][1]['reviewed'] is False
    frame.get_by_role('tab',name='课文',exact=True).click()
    frame.get_by_role('button',name=re.compile('第 2 课')).click()
    frame.get_by_label('背诵内容（选填）',exact=True).fill('第二课背诵内容')
    frame.get_by_role('tab',name='课本信息',exact=True).click()
    frame.get_by_role('tab',name='课文',exact=True).click()
    expect(frame.get_by_label('背诵内容（选填）',exact=True)).to_have_value('第二课背诵内容')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
