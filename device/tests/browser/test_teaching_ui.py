"""Browser contracts; API fixtures are synthetic, not device verification."""
import os
from pathlib import Path
from jinja2 import Environment,FileSystemLoader


def check_ui(width):
    from playwright.sync_api import sync_playwright
    ui=Path(__file__).resolve().parents[2]/'lira'/'ui'
    html=Environment(loader=FileSystemLoader(ui/'templates')).get_template('teaching.html').render()
    row={'id':'one','title':'诗 <img src=x>','version':1,'review_state':'draft','text':'床前明月光','kind':'poem','source':'手动录入'}
    calls=[];conflict=False;paper_attempts=0;hold_save=False;pending_save=None
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
        page=browser.new_page(viewport={'width':width,'height':900})
        def route(r):
            nonlocal paper_attempts,pending_save
            url=r.request.url
            if '/static/' in url:
                path=ui/'static'/url.rsplit('/',1)[1].split('?',1)[0];r.fulfill(body=path.read_text('utf-8'),content_type='text/css' if path.suffix=='.css' else 'application/javascript')
            elif url.endswith('/dev/teaching'):r.fulfill(body=html,content_type='text/html')
            elif url.endswith('/api/teaching'):
                r.fulfill(json={'model_available':False,'capabilities':[{'name':'capture_text','status':'unavailable'},{'name':'probe_tv_receiver','status':'busy'}],'task':{'status':'idle'},'operations':[{'name':'probe_tv_receiver','status':'failed','error':'真实失败'}],'contents':{'contents':[row]},'members':[{'id':1,'name':'孩子'}]})
            elif '/content/' in url:
                if r.request.method=='PUT':
                    calls.append(r.request.post_data_json)
                    if conflict:r.fulfill(status=409,json={'error':'conflict'});return
                    if hold_save:pending_save=r;return
                    row.update(r.request.post_data_json);row['version']+=1
                r.fulfill(json={'content':row})
            elif url.endswith('/records'):
                body=r.request.post_data_json;calls.append(body)
                if body['action']=='paper':
                    paper_attempts+=1
                    if paper_attempts==1:r.abort();return
                r.fulfill(json={'records':[]} if body['action']=='list' else {'record':{'id':'paper'}})
            else:r.fulfill(status=404)
        page.route('http://teaching.test/**',route)
        page.goto('http://teaching.test/dev/teaching')
        assert page.get_by_role('button',name='科目管理（暂未开放）').is_disabled()
        page.get_by_role('button',name='编辑 诗 <img src=x>').click()
        assert page.locator('#contents img').count()==0
        from playwright.sync_api import expect
        expect(page.get_by_label('完整原文')).to_have_value('床前明月光')
        page.get_by_label('完整原文').fill('床前明月光\n疑是地上霜')
        assert page.locator('#confirm-content').is_disabled()
        conflict=True
        page.get_by_role('button',name='保存草稿',exact=True).click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('编辑内容已保留')")
        assert page.get_by_label('完整原文').input_value().endswith('疑是地上霜')
        assert page.locator('#notice').evaluate('(node)=>node===document.activeElement')
        conflict=False
        hold_save=True
        page.get_by_role('button',name='保存草稿',exact=True).click()
        page.wait_for_function("document.querySelector('#text').disabled")
        assert page.get_by_label('完整原文').is_disabled()
        assert page.get_by_label('家庭成员').is_disabled()
        assert page.get_by_role('button',name='保存草稿',exact=True).is_disabled()
        assert pending_save is not None
        row.update(calls[-1]);row['version']+=1
        pending_save.fulfill(json={'content':row})
        hold_save=False
        page.wait_for_function("document.querySelector('#notice').textContent.includes('草稿已保存')")
        assert page.get_by_label('完整原文').is_enabled()
        assert page.get_by_label('完整原文').input_value().endswith('疑是地上霜')
        page.get_by_label('家庭成员').select_option('1')
        page.get_by_label('家长核对结果',exact=True).fill('核对后有两处需改')
        page.get_by_role('button',name='保存家长核对').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('fetch')")
        page.get_by_role('button',name='保存家长核对').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('家长核对已保存')")
        assert any(c.get('action')=='paper' and c['expected_revision']==0 and c['request_id'] for c in calls)
        papers=[c for c in calls if c.get('action')=='paper']
        assert len(papers)==2 and papers[0]==papers[1]
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        evidence=Path(__file__).resolve().parents[3]/'tmp'/'u9-ui'
        evidence.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(evidence/f'teaching-{width}.png'),full_page=True)
        browser.close()


def test_mobile():check_ui(380)
def test_desktop():check_ui(1440)


def test_parent_records_and_content_actions():
    """Exercise parent selections and request identities with synthetic API state."""
    from copy import deepcopy
    from playwright.sync_api import sync_playwright
    ui=Path(__file__).resolve().parents[2]/'lira'/'ui'
    html=Environment(loader=FileSystemLoader(ui/'templates')).get_template('teaching.html').render()
    content={'id':'poem-7','title':'静夜思','version':7,'review_state':'draft','text':'床前明月光','kind':'poem','source':'课本第2页'}
    evidence={'id':'evidence-9','kind':'recitation_evidence','revision':9,'body':{'text':'床前明月光','state':'needs_review','observed_text':'床前明月光','source_version':7}}
    original=deepcopy(evidence)
    records=[{'id':'note-2','kind':'learning_note','revision':2,'body':{'text':'可能还需要复习','state':'proposed_unverified'}},
             {'id':'paper-4','kind':'paper_review','revision':4,'body':{'result':'旧核对结果','note':'原说明'}},evidence]
    calls=[];contents=[content]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
        page=browser.new_page(viewport={'width':1100,'height':900})
        page.on('dialog',lambda dialog:dialog.accept())
        def route(r):
            url=r.request.url;method=r.request.method
            if '/static/' in url:
                path=ui/'static'/url.rsplit('/',1)[1].split('?',1)[0]
                r.fulfill(body=path.read_text('utf-8'),content_type='text/css' if path.suffix=='.css' else 'application/javascript')
            elif url.endswith('/dev/teaching'):r.fulfill(body=html,content_type='text/html')
            elif url.endswith('/api/teaching'):
                r.fulfill(json={'model_available':False,'capabilities':[],'task':{'status':'idle'},'operations':[],'contents':{'contents':contents},'members':[{'id':3,'name':'小朋友'}]})
            elif url.endswith('/api/textbooks'):
                r.fulfill(json={'drafts':[{'id':'book-8','revision':8,'title':'语文上册','page_count':6}]})
            elif url.endswith('/records'):
                body=r.request.post_data_json;calls.append((url,method,deepcopy(body)))
                if body['action']=='paper':
                    row=next(x for x in records if x['id']==body['record_id'])
                    row['revision']+=1;row['body']={'result':body['result'],'note':body['note']}
                elif body['action']=='annotation':
                    records.append({'id':'annotation-1','kind':'recitation_annotation','revision':1,'body':{'evidence_id':body['record_id'],'result':body['result'],'note':body['note']}})
                elif body['action']=='delete':records[:]=[x for x in records if x['id']!=body['record_id']]
                r.fulfill(json={'records':records} if body['action']=='list' else {'record':{'id':'saved'}})
            elif '/content/' in url or url.endswith('/import'):
                body=r.request.post_data_json if method!='GET' else None
                calls.append((url,method,deepcopy(body)))
                if url.endswith('/confirm'):content['review_state']='confirmed'
                elif method=='DELETE':contents.clear()
                elif url.endswith('/import'):
                    content.update(id='imported-1',version=1,review_state='draft',title=body['title'],text='选定第二页\n选定第四页')
                    contents.append(content)
                r.fulfill(json={'content':content})
            else:r.fulfill(status=404)
        page.route('http://teaching.test/**',route)
        page.goto('http://teaching.test/dev/teaching')
        page.get_by_label('家庭成员').select_option('3')
        page.get_by_role('button',name='查看记录',exact=True).click()
        note=page.locator('#records .entry').filter(has_text='可能还需要复习')
        assert note.locator('p').first.inner_text()=='待核实笔记 · 修订 2'
        assert note.get_by_role('button',name='修正纸面核对').count()==0
        page.get_by_role('button',name='修正纸面核对',exact=True).click()
        assert page.get_by_label('家长核对结果',exact=True).input_value()=='旧核对结果'
        page.get_by_label('家长核对结果',exact=True).fill('家长重新核对：第二字需改')
        page.get_by_label('补充说明').fill('对照纸面确认')
        page.get_by_role('button',name='保存家长核对').click()
        page.wait_for_function("document.querySelector('#records').textContent.includes('第二字需改')")
        paper=next(body for _,_,body in calls if body and body.get('action')=='paper')
        assert (paper['member_id'],paper['record_id'],paper['expected_revision'])==(3,'paper-4',4)
        assert paper['request_id'] and paper['note']=='对照纸面确认'
        page.get_by_role('button',name='添加家长修正附注',exact=True).click()
        page.get_by_label('家长核对结果',exact=True).fill('此处是停顿，并非漏字')
        page.get_by_label('补充说明').fill('原始证据保留')
        page.get_by_role('button',name='保存家长核对').click()
        page.wait_for_function("document.querySelector('#records').textContent.includes('此处是停顿')")
        annotation=next(body for _,_,body in calls if body and body.get('action')=='annotation')
        assert (annotation['record_id'],annotation['expected_revision'])==('evidence-9',9)
        assert annotation['request_id']!=paper['request_id']
        assert evidence==original
        assert page.locator('#records .entry').filter(has_text='待核对的背诵证据').count()==1
        evidence_entry=page.locator('#records .entry').filter(has_text='待核对的背诵证据')
        evidence_entry.get_by_text('查看来源与证据',exact=True).click()
        assert evidence_entry.locator('pre').is_visible()
        assert '"source_version": 7' in evidence_entry.locator('pre').inner_text()
        assert '"observed_text": "床前明月光"' in evidence_entry.locator('pre').inner_text()
        page.locator('#records .entry').filter(has_text='家长重新核对：第二字需改').get_by_role('button',name='删除此记录').click()
        page.wait_for_function("!document.querySelector('#records').textContent.includes('第二字需改')")
        deletion=next(body for _,_,body in calls if body and body.get('action')=='delete')
        assert (deletion['record_id'],deletion['expected_revision'])==('paper-4',5)
        page.get_by_role('button',name='编辑 静夜思',exact=True).click()
        page.get_by_role('button',name='确认当前版本原文',exact=True).click()
        page.wait_for_function("document.querySelector('#content-version').textContent.includes('已确认')")
        assert next(body for url,_,body in calls if url.endswith('/confirm'))=={'version':7}
        assert page.locator('#confirm-content').is_disabled()
        page.get_by_role('button',name='删除原文及关联记录',exact=True).click()
        page.wait_for_function("document.querySelector('#content-version').textContent==='尚未保存'")
        assert next(body for url,method,body in calls if method=='DELETE')=={'expected_version':7}
        assert page.locator('#contents .entry').count()==0
        page.get_by_label('标题',exact=True).fill('选页诗词')
        page.get_by_label('类型',exact=True).select_option('poem')
        page.get_by_text('从课本识别页建立原文草稿',exact=True).click()
        page.get_by_role('button',name='读取课本草稿',exact=True).click()
        page.get_by_label('已导入的课本草稿').select_option('book-8')
        page.get_by_label('页码（逗号分隔）').fill('2，4')
        page.get_by_role('button',name='导入为新草稿',exact=True).click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('选定页面')")
        imported=next(body for url,_,body in calls if url.endswith('/import'))
        assert imported=={'draft_id':'book-8','expected_revision':8,'page_numbers':[2,4],'title':'选页诗词','kind':'poem'}
        assert page.get_by_label('完整原文').input_value()=='选定第二页\n选定第四页'
        assert page.locator('#content-version').inner_text()=='版本 1 · 待核对草稿'
        assert page.locator('#confirm-content').is_enabled()
        browser.close()

if __name__=='__main__':test_mobile();test_desktop()
