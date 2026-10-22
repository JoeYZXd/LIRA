import asyncio
import io
import json
from types import SimpleNamespace
import pytest
from PIL import Image
from test_dev_console import FakeRuntime,make_client,login
from lira.dictation_service import DictationService
from lira.textbook_practice import TextbookPractice


def attach(rt,tmp_path):
    books=tmp_path/'books';books.mkdir(exist_ok=True)
    (books/'b.json').write_text(json.dumps(dict(id='b',title='语文',lessons=[dict(unit=1,number=2,title='课',two=['登'],four=['依','尽'],expansions='依靠、尽头、登高',text='原文')]),ensure_ascii=False),encoding='utf8')
    s=DictationService(rt.store._conn,TextbookPractice(books),tmp_path/'dictation',synthesize=lambda text,speed:b'\x01\x00'*240,member_exists=lambda n:n==1,privacy=lambda:rt.privacy.is_on)
    rt.conversation_paths=SimpleNamespace(dictation_service=s)
    rt.personal_memory=SimpleNamespace(people=SimpleNamespace(list_members=lambda:[dict(id=1,name='测试孩子')]))
    return s


@pytest.fixture
def setup(tmp_path):
    rt=FakeRuntime();s=attach(rt,tmp_path);app,client,rt=make_client(runtime=rt)
    with client:yield client,rt,s


def test_parent_guards_versions_and_source_write_readback(setup):
    c,rt,s=setup
    assert c.get('/dev/api/dictation').status_code==401
    for suffix in ('','/task/example','/photo/example'):
        assert c.get('/dev/api/dictation/member/invalid'+suffix).status_code==401
    assert c.post('/dev/api/dictation/member/invalid/task/example/photo',content=b'x').status_code==401
    login(c)
    assert '/dev/static/dictation.js' in c.get('/dev/dictation').text
    index=c.get('/dev/api/dictation').json()
    row={'content':'新教学规则','expected_revision':index['skill']['revision']}
    assert c.post('/dev/api/dictation/skill',json=row,headers={'Origin':'https://evil.test'}).status_code==403
    assert c.post('/dev/api/dictation/skill',json=row).json()['skill']['content']=='新教学规则'
    assert c.post('/dev/api/dictation/skill',json=row).status_code==409
    p=c.post('/dev/api/dictation/profile',json=dict(member_id=1,book_id='b',progress={'lesson':2},expected_revision=0))
    assert p.status_code==200,p.text
    assert c.get('/dev/api/dictation/member/1').json()['profile']['book_id']=='b'
    assert c.post('/dev/api/dictation/profile',json=dict(member_id=True)).status_code==400
    assert c.post('/dev/api/dictation/prepare',json=dict(member_id=1,text='第二课')).status_code==400
    asyncio.run(rt.privacy.set_enabled(True,source='test'))
    assert c.get('/dev/api/dictation').status_code==403


def test_real_result_photo_review_and_next_context(setup):
    c,rt,s=setup;login(c)
    result=asyncio.run(s.prepare_task(dict(scope_type='lesson',start=2,proposals={'2':dict(words=['尽头','依靠'])}),owner_id=1,session_id='task',paired_device_id='tv',callback_registration_id='cb'))
    task=result['task'];tid=task['task_id'];base='/dev/api/dictation/member/1/task/'+tid
    image=io.BytesIO();Image.new('RGB',(20,20)).save(image,format='PNG')
    uploaded=c.post(base+'/photo',content=image.getvalue(),headers={'Content-Type':'image/png'})
    assert uploaded.status_code==200,uploaded.text
    aid=uploaded.json()['photo']['attachment_id']
    assert c.get('/dev/api/dictation/member/1/photo/'+aid).content==image.getvalue()
    iid=task['phases'][1]['items'][0]['item_id']
    data=dict(member_id=1,task_id=tid,status='reviewed_with_errors',errors=[dict(item_id=iid,wrong_characters=['尽'])],expected_revision=0,request_id='review')
    assert c.post('/dev/api/dictation/review',json=data).status_code==200
    details=c.get(base).json()
    assert len(details['photos'])==1 and details['review']['errors'][0]['text']=='尽头'
    assert c.post('/dev/api/dictation/review',json=dict(data,request_id='stale')).status_code==409
    facts=c.post('/dev/api/dictation/facts',json=dict(member_id=1,book_id='b',scope_type='lesson',start=2)).json()
    assert facts['lessons'][0]['facts']['reviews'][0]['errors'][0]['text']=='尽头'
    assert c.get('/dev/api/dictation/member/2/task/'+tid).status_code==400
    assert c.post('/dev/api/dictation/deliver',json=dict(member_id=1,task_id=tid)).json()['delivery_state']=='waiting_tv'


def test_prepare_model_tool_calls_use_real_service_and_bound_parent(setup):
    from openai.types.chat import ChatCompletionMessage
    c,rt,s=setup;login(c)
    replies=[dict(role='assistant',content='第几课？'),dict(role='assistant',tool_calls=[dict(id='ctx',type='function',function=dict(name='get_dictation_context',arguments=json.dumps(dict(scope_type='lesson',start=2))))]),dict(role='assistant',tool_calls=[dict(id='prep',type='function',function=dict(name='prepare_dictation_task',arguments=json.dumps(dict(proposals={'2':dict(words=['尽头','依靠'])}),ensure_ascii=False)))])]
    async def create(**kwargs):return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(**replies.pop(0)))])
    rt._llm=SimpleNamespace(_model='unit-model',_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    first=c.post('/dev/api/dictation/prepare',json=dict(member_id=1,text='准备默写')).json()
    assert first['text']=='第几课？'
    second=c.post('/dev/api/dictation/prepare',json=dict(member_id=1,session_id=first['session_id'],text='第二课')).json()
    assert second['status']=='ready'
    assert s.repository.snapshot(second['task']['task_id'])==second['task']
    assert s.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0]==1


def test_candidate_parent_click_is_required_and_session_is_bound(setup):
    from openai.types.chat import ChatCompletionMessage
    c,rt,s=setup;login(c)
    def tool(name,args,key):return dict(role='assistant',tool_calls=[dict(id=key,type='function',function=dict(name=name,arguments=json.dumps(args,ensure_ascii=False)))])
    args=dict(proposals={'2':dict(words=['依旧','尽头'],confirmed=True)})
    replies=[tool('get_dictation_context',dict(scope_type='lesson',start=2),'ctx'),tool('prepare_dictation_task',args,'prep'),dict(role='assistant',content='请确认采用依旧。'),tool('prepare_dictation_task',args,'again')]
    async def create(**kwargs):return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(**replies.pop(0)))])
    rt._llm=SimpleNamespace(_model='unit',_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    pending=c.post('/dev/api/dictation/prepare',json=dict(member_id=1,text='第二课')).json()
    assert s.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0]==0
    assert pending['pending_candidates']['candidates']==['依旧']
    data=dict(member_id=1,session_id=pending['session_id'],confirmation_id=pending['pending_candidates']['confirmation_id'])
    assert c.post('/dev/api/dictation/confirm_candidates',json=dict(data,confirmation_id='stale')).status_code==400
    final=c.post('/dev/api/dictation/confirm_candidates',json=data).json()
    assert final['status']=='ready'


@pytest.mark.asyncio
async def test_single_worker_transfer_preserves_active_dialogue(tmp_path):
    from lira.dictation_tools import DictationTools
    rt=FakeRuntime();s=attach(rt,tmp_path)
    result=await s.prepare_task(dict(scope_type='lesson',start=2,proposals={'2':dict(words=['尽头','依靠'])}),owner_id=1,session_id='worker',paired_device_id='tv',callback_registration_id='cb')
    tid=result['task']['task_id']
    ui=DictationTools(s,lambda:1,lambda:'ui');voice=DictationTools(s,lambda:1,lambda:'voice')
    ui.adopt(tid)
    ui.coordinator.answer_pending=True
    assert voice.adopt(tid)['status']=='dialogue_active'
    assert s._task_workers[tid] is ui
    ui.coordinator.answer_pending=False
    assert voice.adopt(tid)['status']=='adopted'
    assert ui.coordinator.closed and s._task_workers[tid] is voice
    await ui.close();await voice.close()
