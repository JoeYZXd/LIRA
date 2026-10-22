import io
from PIL import Image
import pytest
from test_dev_console import make_client, login


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('LIRA_TEXTBOOK_DIR',str(tmp_path/'books'))
    monkeypatch.setenv('LIRA_TEXTBOOK_IMPORT_DIR',str(tmp_path/'drafts'))
    app,client,rt=make_client()
    with client:
        yield client


def upload(client):
    image=Image.new('RGB',(20,30),'white');data=io.BytesIO();image.save(data,format='PNG')
    return client.post('/dev/api/textbooks/import',files=[('files',('page.png',data.getvalue(),'image/png'))])


def test_import_requires_authenticated_session(client):
    assert client.get('/dev/api/textbooks').status_code==401
    assert upload(client).status_code==401
    assert client.get('/dev/api/textbooks/drafts/'+'a'*32+'/pages/1/thumbnail').status_code==401


def test_authenticated_page_thumbnail(client):
    login(client);draft=upload(client).json()['draft']
    url='/dev/api/textbooks/drafts/'+draft['id']+'/pages/'
    response=client.get(url+'1/thumbnail')
    assert response.status_code==200 and response.headers['content-type']=='image/jpeg'
    assert response.headers['cache-control']=='no-store'
    with Image.open(io.BytesIO(response.content)) as image:assert image.width<=160 and image.height<=220
    assert client.get(url+'0/thumbnail').status_code==400


def test_ui_documents_reference_current_asset_content_versions(client):
    import hashlib
    import re
    login(client)
    for path in ('/dev','/dev/textbooks?embedded=1'):
        response=client.get(path)
        assert response.headers['cache-control']=='no-store'
        assets=re.findall(r'(?:src|href)="(/dev/static/[^"?]+)\?v=([a-f0-9]{16})"',response.text)
        assert assets
        for url,version in assets:
            raw=client.get(url+'?v='+version)
            assert raw.status_code==200
            assert hashlib.sha256(raw.content).hexdigest()[:16]==version


def test_upload_review_publish_and_voice_library(client,tmp_path):
    login(client)
    response=upload(client);assert response.status_code==201,response.text
    draft=response.json()['draft'];base='/dev/api/textbooks/drafts/'+draft['id']
    assert client.get(base+'/pages/1').headers['content-type']=='image/png'
    assert client.get('/dev/api/textbooks').json()['books']==[]
    row={'number':1,'unit':1,'title':'第一课','four':['日'],'two':['月'],'source_pages':[1],'reviewed':False}
    draft=client.put(base,json={'revision':draft['revision'],'title':'测试教材','lessons':[row]}).json()['draft']
    assert client.post(base+'/publish',json={'revision':draft['revision'],'confirmed':True}).status_code==400
    row['reviewed']=True
    draft=client.put(base,json={'revision':draft['revision'],'title':'测试教材','lessons':[row]}).json()['draft']
    assert client.post(base+'/publish',json={'revision':draft['revision'],'confirmed':True}).status_code==200
    from lira.textbook_practice import TextbookPractice
    books=TextbookPractice(tmp_path/'books').books()
    assert books[0]['lessons'][0]['four']==['日']


def test_revision_and_origin_fail_closed(client):
    login(client);draft=upload(client).json()['draft'];base='/dev/api/textbooks/drafts/'+draft['id']
    assert client.put(base,json={'revision':-1,'title':'x','lessons':[]}).status_code==409
    assert client.put(base,headers={'Origin':'https://outside.example'},json={'revision':draft['revision'],'title':'x','lessons':[]}).status_code==403


def test_published_book_detail_and_revision_guard(client):
    assert client.get('/dev/api/textbooks/books/legacy').status_code==401
    assert client.post('/dev/api/textbooks/books/legacy/revise').status_code==401
    login(client);draft=upload(client).json()['draft'];base='/dev/api/textbooks/drafts/'+draft['id']
    row={'number':1,'unit':1,'title':'第一课','four':['日'],'two':['月'],'source_pages':[1],'reviewed':True,'expansions':'日：日子','text':''}
    draft=client.put(base,json={'revision':draft['revision'],'title':'测试教材','lessons':[row],'cover_page':1}).json()['draft']
    assert client.post(base+'/publish',json={'revision':draft['revision'],'confirmed':True}).status_code==200
    url='/dev/api/textbooks/books/'+draft['id']
    view=client.get(url).json()['book']
    assert view['lessons'][0]['expansions']=='日：日子' and view['cover_page']==1
    assert client.post(url+'/revise',headers={'Origin':'https://outside.example'}).status_code==403
    revised=client.post(url+'/revise').json()['draft']
    assert revised['id']!=draft['id'] and revised['lessons'][0]['reviewed'] is False
    assert client.put(url+'/cover',headers={'Origin':'https://outside.example'},json={'revision':view['source_revision'],'cover_page':None}).status_code==403
    result=client.put(url+'/cover',json={'revision':view['source_revision'],'cover_page':None})
    assert result.status_code==200 and result.json()['book']['cover_page'] is None


def test_partial_review_stays_editable_on_shelf_and_available_to_voice(client,tmp_path,monkeypatch):
    monkeypatch.setenv('LIRA_TEXTBOOK_IMPORT_DIR',str(tmp_path/'textbook-imports'))
    login(client)
    draft=upload(client).json()['draft'];url='/dev/api/textbooks/drafts/'+draft['id']
    rows=[{'number':7,'unit':2,'title':'第七课','four':['日'],'two':['水'],'source_pages':[1],'reviewed':True},
          {'number':8,'unit':2,'title':'第八课','four':[],'two':[],'source_pages':[],'reviewed':False}]
    saved=client.put(url,json={'revision':draft['revision'],'title':'部分核对教材','lessons':rows}).json()['draft']
    index=client.get('/dev/api/textbooks')
    assert index.json()['books']==[]  # Shelf must route this ID through /drafts, not /books.
    assert index.json()['drafts'][0]['id']==saved['id']
    assert index.headers['cache-control']=='no-store'
    reopened=client.get(url)
    assert reopened.status_code==200 and reopened.json()['draft']['revision']==saved['revision']
    from lira.textbook_practice import TextbookPractice
    voice=TextbookPractice(tmp_path/'books',draft_directory=tmp_path/'textbook-imports')
    assert voice.catalog()['books'][0]['book_id']==draft['id']
    assert voice.prepare({'scope_type':'lesson','start':7,'character_group':'four'})['status']=='ready'
    assert voice.prepare({'scope_type':'lesson','start':8,'character_group':'four'})['status']=='lesson_review_required'
