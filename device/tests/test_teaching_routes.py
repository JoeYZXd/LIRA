from types import SimpleNamespace
import pytest
from test_dev_console import make_client, login, FakeRuntime
from lira.teaching_content import TeachingContentRepository
from lira.agent_task_state import AgentTaskState
from lira.realtime_capabilities import CapabilityRegistry


@pytest.fixture
def setup():
    rt=FakeRuntime()
    rt.conversation_paths=SimpleNamespace(teaching_content=TeachingContentRepository(rt.store._conn),
        capabilities_enabled=False,session=None,capabilities=CapabilityRegistry(),agent_task=AgentTaskState())
    app,client,rt=make_client(runtime=rt)
    with client:yield client,rt


def test_guards_and_real_disabled_status(setup):
    client,rt=setup
    assert client.get('/dev/api/teaching').status_code==401
    login(client)
    assert client.get('/dev/teaching').status_code==200
    assert client.get('/dev/api/teaching').json()['model_available'] is False
    from lira.realtime_capabilities import CapabilityExecutor
    p=rt.conversation_paths
    p.capabilities.register('busy_probe','test',{'type':'object','properties':{}},lambda args:None,availability=lambda:'busy')
    p.capabilities_enabled=True
    p.session=SimpleNamespace(closed=False,failed=False,capabilities=CapabilityExecutor(p.capabilities))
    snapshot=client.get('/dev/api/teaching').json()
    assert snapshot['model_available'] is True and snapshot['capabilities'][0]['status']=='busy'
    p.session.failed=True
    assert client.get('/dev/api/teaching').json()['capabilities'][0]['status']=='unavailable'
    data={'title':'诗','text':'床前明月光','kind':'poem','source':'手动录入'}
    assert client.post('/dev/api/teaching/content',headers={'Origin':'https://evil.test'},json=data).status_code==403
    import asyncio
    asyncio.run(rt.privacy.set_enabled(True,source='test'))
    assert client.post('/dev/api/teaching/content',json=data).status_code==403


def test_draft_confirm_version_and_model_read(setup):
    client,rt=setup;login(client)
    data={'title':'静夜思','text':'床前明月光','kind':'poem','source':'手动录入'}
    row=client.post('/dev/api/teaching/content',json=data).json()['content']
    base='/dev/api/teaching/content/'+row['id']
    assert rt.conversation_paths.teaching_content.read(row['id'])['status']!='ok'
    assert client.post(base+'/confirm',json={'version':True}).status_code==400
    assert client.post(base+'/confirm',json={'version':0}).status_code==409
    assert client.post(base+'/confirm',json={'version':1}).status_code==200
    assert rt.conversation_paths.teaching_content.read(row['id'])['version']==1
    assert client.get(base).json()['content']['text']=='床前明月光'


@pytest.mark.asyncio
async def test_content_withdrawal_closes_old_cloud_before_edit_and_purge():
    from test_conversation_paths import runtime
    rt,p=runtime()
    rt.store._conn.execute('CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY)')
    row=p.teaching_content.save_draft(title='诗',text='床前明月光',kind='poem',source='manual')
    p.teaching_content.confirm(row['id'],1)
    await p.switch('doubao_realtime');await p.start();session=p.session
    old=p.teaching_memory.purge_content
    def purge(cid):
        assert session.closed and p.session is None
        return old(cid)
    p.teaching_memory.purge_content=purge
    updated=await p.manage_teaching_content(row['id'],{'expected_version':1,'title':'诗','text':'疑是地上霜','kind':'poem','source':'家长改正'})
    assert updated['version']==2 and updated['review_state']=='draft'
    assert p.teaching_content.read(row['id'])['status']=='draft'
    with pytest.raises(ValueError,match='conflict'):
        await p.manage_teaching_content(row['id'],{'expected_version':1},delete=True)
    assert (await p.manage_teaching_content(row['id'],{'expected_version':2},delete=True))['status']=='deleted'
    await p.close()


def test_parent_review_routes_persist_retry_and_conflict():
    from test_teaching_memory import service
    memory,task,identity,source=service()
    rt=FakeRuntime();rt.personal_memory=source.personal_memory
    rt.conversation_paths=SimpleNamespace(teaching_memory=memory)
    app,client,rt=make_client(runtime=rt)
    with client:
        login(client)
        data={'member_id':identity.member_id,'action':'paper','record_id':None,'expected_revision':0,'request_id':'paper-one','result':'家长核对','note':''}
        url='/dev/api/teaching/records'
        assert client.post(url,json=dict(data,member_id=True)).status_code==400
        assert client.post(url,json=data,headers={'Origin':'https://other.test'}).status_code==403
        first=client.post(url,json=data);assert first.status_code==200,first.text
        assert client.post(url,json=data).json()==first.json()
        assert client.post(url,json=dict(data,result='伪造重试')).status_code==409
        row=first.json()['record']
        assert client.post(url,json=dict(data,record_id=row['id'],request_id='stale')).status_code==409
        assert client.post(url,json={'member_id':identity.member_id,'action':'list'}).json()['records'][0]['id']==row['id']


@pytest.mark.asyncio
async def test_parent_delete_owner_revision_annotations_and_repeated_delete():
    from test_teaching_memory import service
    from lira.teaching_evidence import TeachingEvidence
    memory,task,identity,rt=service();repo=TeachingContentRepository(rt.store._conn)
    source=repo.save_draft(title='诗',text='床前明月光',kind='poem',source='manual');repo.confirm(source['id'],1)
    result=await TeachingEvidence(memory,repo,lambda:[]).compare({'content_id':source['id'],'version':1,'turn_id':task.turn['id']})
    mid=identity.member_id
    memory.parent_annotate_recitation(member_id=mid,evidence_id=result['id'],expected_revision=1,request_id='annotation',result='已人工核对',note='')
    other=rt.personal_memory.people.create_member('其他孩子')['id']
    with pytest.raises(ValueError):memory.parent_delete(other,result['id'],1)
    with pytest.raises(ValueError):memory.parent_delete(mid,result['id'],2)
    assert memory.parent_delete(mid,result['id'],1)['deleted_records']==2
    assert memory.parent_records(mid)==[]
    with pytest.raises(ValueError):memory.parent_delete(mid,result['id'],1)


def test_selected_import_pages_are_draft_and_revision_is_checked(setup,tmp_path,monkeypatch):
    import io
    from PIL import Image
    from lira.textbook_import import TextbookImportStore
    books=tmp_path/'books';imports=tmp_path/'imports'
    monkeypatch.setenv('LIRA_TEXTBOOK_DIR',str(books));monkeypatch.setenv('LIRA_TEXTBOOK_IMPORT_DIR',str(imports))
    store=TextbookImportStore(imports,books)
    data=io.BytesIO();Image.new('RGB',(10,10),'white').save(data,format='PNG')
    row=store.create([('page.png',data.getvalue())]);store.set_recognizing(row['id'])
    row=store.update_page_text(row['id'],1,'床前明月光')
    client,rt=setup;login(client)
    body={'draft_id':row['id'],'expected_revision':row['revision'],'page_numbers':[1],'title':'静夜思','kind':'poem'}
    assert client.post('/dev/api/teaching/import',json=dict(body,expected_revision=True)).status_code==400
    assert client.post('/dev/api/teaching/import',json=dict(body,expected_revision=row['revision']-1)).status_code==409
    saved=client.post('/dev/api/teaching/import',json=body)
    assert saved.status_code==200,saved.text
    assert saved.json()['content']['review_state']=='draft'
    assert saved.json()['content']['text']=='床前明月光'


@pytest.mark.asyncio
async def test_privacy_while_waiting_for_management_lock_or_cloud_stop_blocks_writes():
    import asyncio
    import httpx
    from test_conversation_paths import runtime
    from test_dev_console import PASSPHRASE
    source,p=runtime();source.store._conn.execute('CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY)')
    rt=FakeRuntime();rt.privacy=source.privacy;rt.conversation_paths=p
    app,unused,rt=make_client(runtime=rt)
    entered=asyncio.Event();release=asyncio.Event();old_stop=p._stop
    async def held_stop():
        entered.set();await release.wait();await old_stop()
    p._stop=held_stop
    row=p.teaching_content.save_draft(title='诗',text='床前明月光',kind='poem',source='manual')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        assert (await client.post('/dev/login',json={'passphrase':PASSPHRASE})).status_code==200
        edit={'expected_version':1,'title':'诗','text':'疑是地上霜','kind':'poem','source':'manual'}
        first=asyncio.create_task(client.put('/dev/api/teaching/content/'+row['id'],json=edit))
        await entered.wait()
        queued=asyncio.create_task(client.post('/dev/api/teaching/content',json={k:v for k,v in edit.items() if k!='expected_version'}))
        await asyncio.sleep(0)
        await rt.privacy.set_enabled(True,source='test')
        release.set()
        assert (await first).status_code==403
        assert (await queued).status_code==403
        assert p.teaching_content.get_draft(row['id'])['version']==1
        assert len(p.teaching_content.list()['contents'])==1
