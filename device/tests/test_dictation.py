import json
import pytest
from lira.textbook_practice import TextbookPractice


@pytest.mark.asyncio
async def test_dictation_preparation_can_launch_but_ordinary_probe_cannot():
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    class Sender:
        def __init__(self):self.preparations=0;self.probes=0
        async def probe(self):
            self.probes+=1
            return {'interactive':True,'foreground':False}
        async def ensure_ready(self):
            self.preparations+=1
            return {'interactive':True,'foreground':True}
    sender=Sender();tv=TvVoiceTools(sender=sender);d=DictationTools(tv)
    assert (await tv.probe_receiver())['foreground'] is False
    assert sender.preparations==0
    assert (await d.call('check_dictation_tv',{}))['status']=='ready'
    assert sender.preparations==1 and sender.probes==1
    await d.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('probe,status,reason',[
    ({'interactive':True,'foreground':True},'ready',None),
    ({'interactive':False,'foreground':False},'needs_input','未亮屏'),
    ({'interactive':True,'foreground':False},'needs_input','不在前台'),
    ({},'needs_input','无法确认'),
])
async def test_tv_readiness_uses_receiver_without_spoken_confirmation(probe,status,reason):
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    class Sender:
        async def probe(self):return probe
        async def show(self,payload):return {'foreground':True,'rendered_id':payload['id']}
    tv=TvVoiceTools(sender=Sender());d=DictationTools(tv)
    assert (await d.call('show_dictation_ready',{'lesson':7}))['status']=='check_required'
    d.event('user_text','默写第7课')
    result=await d.call('check_dictation_tv',{})
    assert result['status']==status and result['requested_lesson']==7
    assert 'question' not in result
    if reason:assert reason in result['reason']
    if status=='ready':
        assert (await d.call('show_dictation_ready',{'lesson':7}))['status']=='displaying'
        await tv.task
        assert (await tv.call('get_tv_display_status',{}))['status']=='displayed'
    await d.close();await tv.close()


def test_reviewed_lesson_available_without_publishing_book(tmp_path):
    books=tmp_path/'books'; books.mkdir()
    drafts=tmp_path/'imports'; folder=drafts/('a'*32); folder.mkdir(parents=True)
    (folder/'draft.json').write_text(json.dumps({'id':'a'*32,'title':'语文二年级','status':'review','revision':2,
        'lessons':[{'unit':2,'number':7,'title':'第七课','four':['日'],'two':['月'],'reviewed':True},
                   {'unit':2,'number':8,'title':'未核对','four':['山'],'two':[],'reviewed':False}]}),encoding='utf-8')
    practice=TextbookPractice(books, draft_directory=drafts)
    catalog=practice.catalog()['books']
    assert len(catalog)==1
    assert catalog[0]['lessons'][0]['reviewed'] is True
    assert catalog[0]['lessons'][1]['reviewed'] is False
    assert practice.prepare({'start':7,'scope_type':'lesson','character_group':'four'})['status']=='ready'
    assert practice.prepare({'start':8,'scope_type':'lesson','character_group':'four'})['status']=='lesson_review_required'
    assert list(books.iterdir())==[]


@pytest.mark.asyncio
async def test_dictation_material_repeat_and_pinyin_pages(tmp_path):
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    chars=list('日水山田禾米口云牛天')
    (tmp_path/'book.json').write_text(json.dumps({'id':'b','title':'语文','lessons':[
        {'unit':1,'number':7,'title':'七','four':['日','月'],'two':chars}]}),encoding='utf-8')
    spoken=[]
    async def speak(text):
        spoken.append(text)
        return {'status':'queued'}
    from test_tv_voice import Sender
    d=DictationTools(TvVoiceTools(sender=Sender(),textbook_dir=tmp_path), speak=speak)
    d.tv.event('user_text','四会字和二会字都练')
    material=await d.call('get_dictation_material',{'book_id':'b','lesson':7})
    assert material['status']=='ready'
    assert material['four']==['日','月']
    result=await d.call('present_dictation_item',{'material_id':material['material_id'],'group':'four','offset':0})
    await d.speech_task
    assert result['status']=='queued' and spoken==['日。日。日。']
    assert d.tv.pending is None
    again=await d.call('get_dictation_material',{'lesson':7})
    assert again['material_id']==material['material_id']
    assert (await d.call('present_dictation_item',{'material_id':again['material_id'],'group':'four','offset':1}))['status']=='awaiting_user'
    result=await d.call('present_dictation_item',{'material_id':material['material_id'],'group':'two','offset':0})
    assert result['item_count']==8 and result['has_more'] is True
    assert '日' not in result['preview'] and 'rì' in result['preview']
    assert (await d.call('present_dictation_item',{'material_id':material['material_id'],'group':'two','offset':8}))['status']=='awaiting_user'
    d.event('user_text','不要显示更多')
    assert (await d.call('present_dictation_item',{'material_id':material['material_id'],'group':'two','offset':8}))['status']=='awaiting_user'
    d.event('assistant_text','要显示下一页拼音吗？');d.event('response_done','');d.event('user_text','好的')
    result=await d.call('present_dictation_item',{'material_id':material['material_id'],'group':'two','offset':8})
    assert result['item_count']==2 and result['has_more'] is False
    assert (await d.call('present_dictation_item',{'material_id':'invented','group':'four','offset':0}))['status']=='stale_material'

@pytest.mark.asyncio
async def test_tv_actions_are_attempted_and_failure_is_not_success():
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    class Offline:
        def __init__(self):self.probes=0;self.sends=[]
        async def probe(self):self.probes+=1;raise ConnectionError()
        async def show(self,payload):self.sends.append(payload);raise ConnectionError()
    sender=Offline(); d=DictationTools(TvVoiceTools(sender=sender))
    d.event('user_text','给开心默写第七课')
    result=await d.call('check_dictation_tv',{})
    assert sender.probes==1 and result['receiver']['status']=='unavailable'
    assert result['status']=='needs_input' and '无法确认' in result['reason']
    assert 'question' not in result
    assert (await d.call('show_dictation_ready',{'lesson':17}))['status']=='lesson_mismatch'
    assert sender.sends==[]
    assert (await d.call('show_dictation_ready',{'lesson':7}))['status']=='displaying'
    await d.tv.task
    assert sender.sends[0]['text']=='准备开始默写第7课'
    assert (await d.tv.call('get_tv_display_status',{}))['status']=='failed'
    await d.close();await d.tv.close()


@pytest.mark.asyncio
async def test_multiple_book_choice_requires_observed_next_turn_consent(tmp_path):
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    for key in ('a','b'):
        (tmp_path/f'{key}.json').write_text(json.dumps({'id':key,'title':f'语文{key}',
            'lessons':[{'unit':1,'number':7,'title':'七','four':['日'],'two':['水']}]}),encoding='utf-8')
    saved=[]; d=DictationTools(TvVoiceTools(textbook_dir=tmp_path),remember=saved.append)
    args={'book_id':'b','lesson':7}
    assert (await d.call('get_dictation_material',args))['status']=='confirmation_required'
    assert (await d.call('confirm_dictation_book_choice',{'book_id':'b'}))['status']=='confirmation_required'
    d.event('assistant_text','这次用《语文b》默写，可以吗？'); d.event('response_done','')
    d.event('user_text','可以')
    assert (await d.call('confirm_dictation_book_choice',{'book_id':'b'}))['status']=='confirmed'
    assert saved==['b']
    assert (await d.call('get_dictation_material',args))['status']=='ready'
    await d.close()


@pytest.mark.asyncio
async def test_default_runtime_rejects_legacy_dictation_without_audio():
    from lira.audio.doubao_realtime import RealtimeSession, RealtimeConfig
    sent=[]
    class Socket:
        async def send(self,data):sent.append(json.loads(data))
        async def close(self):pass
    class Player:
        async def play_stream(self,chunks):raise AssertionError('legacy audio must not play')
        async def stop(self):pass
        async def close(self):pass
    async def legacy_handler(name,args):raise AssertionError('legacy handler must not run')
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *a:None,on_tool=legacy_handler)
    session.ws=Socket()
    try:
        await session._tool({'items':[{'name':'present_dictation_item','call_id':'one','arguments':'{}'}]})
        result=json.loads(sent[-1]['items'][0]['content'][0]['text'])
        assert result=={'status':'unavailable','reason':'legacy_dictation_disabled'}
        assert not any(e['type']=='speech_text_buffer.commit' for e in sent)
    finally:await session.close()

@pytest.mark.asyncio
async def test_stop_cancels_only_owned_tv_send():
    import asyncio
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    class Delayed:
        async def probe(self):return {}
        async def show(self,payload):await asyncio.Event().wait()
    tv=TvVoiceTools(sender=Delayed());d=DictationTools(tv)
    result=await d.call('check_dictation_tv',{})
    await d.call('show_dictation_ready',{'lesson':7});task=tv.task
    await asyncio.sleep(0)
    d.event('user_text','结束默写');await asyncio.gather(task,return_exceptions=True)
    assert task.cancelled()
    await d.close();await tv.close()


@pytest.mark.asyncio
async def test_four_character_heteronym_uses_model_context_without_repeat_consent(tmp_path):
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    (tmp_path/'b.json').write_text(json.dumps({'id':'b','title':'语文','lessons':[
        {'unit':1,'number':7,'title':'七','four':['长'],'two':[]}]}),encoding='utf-8')
    spoken=[]
    async def speak(text):spoken.append(text);return {'status':'completed'}
    d=DictationTools(TvVoiceTools(textbook_dir=tmp_path),speak=speak)
    d.tv.event('user_text','四会字')
    material=await d.call('get_dictation_material',{'lesson':7})
    args={'material_id':material['material_id'],'group':'four','offset':0}
    assert (await d.call('present_dictation_item',args))['status']=='pronunciation_context_required'
    args['pronunciation_hint']='读cháng，长江的长'
    assert (await d.call('present_dictation_item',args))['status']=='queued'
    await d.speech_task
    assert spoken==['长江的长。长江的长。长江的长。']
    await d.close()

@pytest.mark.asyncio
async def test_generic_display_cannot_bypass_active_dictation_material_gate(tmp_path):
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    from test_tv_voice import Sender
    (tmp_path/'b.json').write_text(json.dumps({'id':'b','title':'语文','lessons':[
        {'unit':1,'number':7,'title':'七','four':['日'],'two':['水']}]}),encoding='utf-8')
    tv=TvVoiceTools(sender=Sender(),textbook_dir=tmp_path);d=DictationTools(tv)
    m=await d.call('get_dictation_material',{'lesson':7})
    assert (await tv.call('prepare_tv_display',{'content_type':'text','text':'日'}))['status']=='dictation_tool_required'
    tv.event('user_text','二会字')
    result=await d.call('present_dictation_item',{'material_id':m['material_id'],'group':'two','offset':0})
    assert result['status']=='awaiting_consent' and result['item_count']==1
    tv.event('assistant_text','可以把这段拼音显示到电视上吗？');tv.event('response_done','');tv.event('user_text','可以')
    assert (await tv.call('confirm_tv_display',{'request_id':result['request_id']}))['status']=='displaying'
    d.note_tv_tool('confirm_tv_display',{'request_id':result['request_id']},{'status':'displaying'})
    await tv.task
    assert tv.sender.sent[0]['text']=='shuǐ'
    await d.call('cancel_dictation',{})
    assert (await tv.call('prepare_tv_display',{'content_type':'text','text':'其他任务'}))['status']=='awaiting_consent'
    await tv.close()


def test_practice_category_can_be_negotiated_and_homophones_are_contextual():
    from lira.tv_voice import TvVoiceTools
    tv=TvVoiceTools()
    tv.event('assistant_text','这次练二会字，对吗？');tv.event('response_done','');tv.event('user_text','好的')
    assert tv.practice_group=='two'
    tv.event('user_text','这次练思慧姿')
    assert tv.practice_group=='four'
    tv.event('user_text','不是四会字')
    assert tv.practice_group is None

@pytest.mark.asyncio
async def test_book_preference_requires_confirmed_owner_and_is_cleared_on_forget():
    from test_conversation_paths import runtime
    rt,paths=runtime()
    rt.store._conn.execute('CREATE TABLE IF NOT EXISTS members (id INTEGER PRIMARY KEY)')
    rt.store._conn.executemany('INSERT INTO members VALUES(?)',[(1,),(2,)]);rt.store._conn.commit()
    try:
        paths._remember_dictation_book('book-a')
        assert paths._recall_dictation_book()['status']=='unavailable'
        paths.speaker_input.state='recognized';paths.speaker_input.member_id=1
        paths._remember_dictation_book('book-a')
        assert paths._recall_dictation_book()['book_id']=='book-a'
        paths.speaker_input.member_id=2
        assert paths._recall_dictation_book()['book_id'] is None
        paths.speaker_input.member_id=1
        await paths.forget_member_now(1)
        assert rt.store.get_meta('dictation_book:1','')==''
    finally:await paths.close()


def test_dictation_tools_are_sensitive_without_enabling_extensions():
    from lira.dictation import DICTATION_TOOL_NAMES
    from lira.voice_trace import is_teaching_tool,minimize_teaching
    assert all(is_teaching_tool(name) for name in DICTATION_TOOL_NAMES)
    raw={'name':'get_dictation_material','result':{'four':['私'],'book_title':'家庭测试'}}
    assert '家庭测试' not in json.dumps(minimize_teaching(raw),ensure_ascii=False)

@pytest.mark.asyncio
async def test_two_character_reading_is_model_selected_but_must_be_valid(tmp_path):
    from lira.dictation import DictationTools
    from lira.tv_voice import TvVoiceTools
    from test_tv_voice import Sender
    (tmp_path/'b.json').write_text(json.dumps({'id':'b','title':'语文','lessons':[
        {'unit':1,'number':7,'title':'七','four':[],'two':['长','月']}]}),encoding='utf-8')
    tv=TvVoiceTools(sender=Sender(),textbook_dir=tmp_path);d=DictationTools(tv)
    tv.event('user_text','二会字')
    m=await d.call('get_dictation_material',{'lesson':7})
    args={'material_id':m['material_id'],'group':'two','offset':0,'pinyin':'invalid|wrong'}
    assert (await d.call('present_dictation_item',args))['status']=='pronunciation_context_required'
    assert tv.pending is None
    args['pinyin']='cháng|yuè'
    result=await d.call('present_dictation_item',args)
    assert result['status']=='awaiting_consent' and result['preview']=='cháng    yuè'
    assert not tv.sender.sent  # Display consent is still separate from pronunciation choice.
    await d.close();await tv.close()
