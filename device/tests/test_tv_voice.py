import asyncio

import pytest

from lira.tv_voice import TvVoiceTools


class Sender:
    def __init__(self):self.sent=[]
    async def show(self, payload):
        self.sent.append(payload)
        return {'foreground':True,'rendered_id':payload['id']}


def make():
    sender=Sender()
    tv=TvVoiceTools(sender=sender, image_dir=None)
    return tv,sender


async def prepare(tv):
    tv.event('user_text','请显示一段话')
    return await tv.call('prepare_tv_text',{'text':'你好\n世界'})


def answer(tv, result, text='同意显示'):
    tv.event('assistant_text',result['question'])
    tv.event('response_done','')
    tv.event('user_text',text)


@pytest.mark.asyncio
async def test_must_ask_then_observe_next_real_transcript():
    tv,sender=make();result=await prepare(tv)
    assert not sender.sent
    assert (await tv.call('confirm_tv_display',{'request_id':result['request_id']}))['status']=='confirmation_required'
    answer(tv,result)
    assert (await tv.call('confirm_tv_display',{'request_id':result['request_id']}))['status']=='displaying'
    await tv.task
    assert sender.sent[0]['text']=='你好\n世界'
    assert (await tv.call('get_tv_display_status',{}))['status']=='displayed'
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    assert len(sender.sent)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('reply',['不要','不用了','等一下','可以但是别显示','他说可以','明天再说',''])
async def test_no_or_ambiguous_never_sends(reply):
    tv,sender=make();result=await prepare(tv);answer(tv,result,reply)
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    assert not sender.sent


@pytest.mark.asyncio
async def test_yes_before_question_and_replaced_payload_are_not_authorization():
    tv,sender=make();result=await prepare(tv)
    tv.event('user_text','同意显示')
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    assert not sender.sent
    result=await prepare(tv);answer(tv,result)
    changed=await tv.call('prepare_tv_text',{'text':'不同的内容'})
    await tv.call('confirm_tv_display',{'request_id':changed['request_id']})
    assert not sender.sent


@pytest.mark.asyncio
async def test_expiry_and_session_close_clear_consent():
    tv,sender=make();result=await prepare(tv);answer(tv,result)
    tv.pending['expires']=0
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    assert not sender.sent
    result=await prepare(tv);answer(tv,result);await tv.close()
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    assert not sender.sent


@pytest.mark.asyncio
async def test_image_snapshot_is_bound_to_consent(tmp_path):
    sender=Sender();tv=TvVoiceTools(sender=sender,image_dir=tmp_path)
    (tmp_path/'sample.png').write_bytes(b'\x89PNG\r\n\x1a\noriginal')
    result=await tv.call('prepare_tv_image',{'image_id':'sample.png','caption':'测试图片'})
    (tmp_path/'sample.png').write_bytes(b'changed')
    answer(tv,result)
    await tv.call('confirm_tv_display',{'request_id':result['request_id']});await tv.task
    import base64
    assert base64.b64decode(sender.sent[0]['image_base64']).endswith(b'original')
    with pytest.raises(ValueError):await tv.call('prepare_tv_image',{'image_id':'../secret.png','caption':''})


@pytest.mark.asyncio
async def test_close_cancels_inflight_send():
    started=asyncio.Event();sent=[]
    class Slow:
        async def show(self,payload):
            started.set();await asyncio.sleep(100);sent.append(payload)
    tv=TvVoiceTools(sender=Slow(),image_dir=None)
    result=await prepare(tv);answer(tv,result)
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    await started.wait();await tv.close()
    assert not sent and tv.task is None


@pytest.mark.asyncio
async def test_real_conversation_events_dispatch_and_stop_clear_consent():
    from test_conversation_paths import runtime
    rt,paths=runtime()
    sender=Sender();paths.tv=TvVoiceTools(sender=sender)
    paths._event('user_text','请显示你好')
    result=await paths._tool('prepare_tv_text',{'text':'你好'})
    paths._event('assistant_text',result['question'])
    paths._event('response_done','')
    paths._event('user_text','同意显示')
    await paths._tool('confirm_tv_display',{'request_id':result['request_id']})
    await paths.tv.task
    assert len(sender.sent)==1
    await paths.close()
    assert paths.tv.pending is None


@pytest.mark.asyncio
async def test_privacy_blocks_real_dispatch():
    from test_conversation_paths import runtime
    rt,paths=runtime();paths.tv=TvVoiceTools(sender=Sender())
    await rt.privacy.set_enabled(True,source='test')
    with pytest.raises(PermissionError):await paths._tool('prepare_tv_text',{'text':'private'})
    await paths.close()


@pytest.mark.asyncio
async def test_interrupted_question_cannot_be_rearmed_by_late_done():
    tv,sender=make();result=await prepare(tv)
    tv.event('assistant_text',result['question']);tv.event('interrupted','')
    tv.event('response_done','');tv.event('user_text','同意显示')
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    assert not sender.sent


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['不用了','不要','等一下','取消显示'])
async def test_spoken_withdrawal_cancels_inflight(text):
    entered=asyncio.Event()
    class Slow:
        async def show(self,payload):entered.set();await asyncio.sleep(100)
    tv=TvVoiceTools(sender=Slow());result=await prepare(tv);answer(tv,result)
    await tv.call('confirm_tv_display',{'request_id':result['request_id']});await entered.wait()
    tv.event('user_text',text)
    await asyncio.gather(tv.task,return_exceptions=True)
    assert tv.result['status']=='cancelled'


@pytest.mark.asyncio
async def test_generic_display_collects_missing_parameters_before_consent():
    tv,sender=make()
    result=await tv.call('prepare_tv_display',{})
    assert result['status']=='needs_input'
    assert result['missing_parameters']==['content_type']
    result=await tv.call('prepare_tv_display',{'content_type':'text'})
    assert result['missing_parameters']==['text']
    assert tv.pending is None and not sender.sent
    result=await tv.call('prepare_tv_display',{'content_type':'text','text':'今天一起读书'})
    answer(tv,result)
    await tv.call('confirm_tv_display',{'request_id':result['request_id']})
    await tv.task
    assert sender.sent[0]['text']=='今天一起读书'


@pytest.mark.asyncio
async def test_incomplete_new_request_invalidates_old_consent():
    tv,sender=make();old=await prepare(tv);answer(tv,old)
    result=await tv.call('prepare_tv_display',{'content_type':'image'})
    assert result['missing_parameters']==['image_id']
    await tv.call('confirm_tv_display',{'request_id':old['request_id']})
    assert not sender.sent and tv.pending is None


@pytest.mark.asyncio
async def test_generic_image_uses_catalog_and_optional_caption(tmp_path):
    tv=TvVoiceTools(sender=Sender(),image_dir=tmp_path)
    result=await tv.call('prepare_tv_display',{'content_type':'image','image_id':'missing.png'})
    assert result['status']=='needs_input' and tv.pending is None
    (tmp_path/'sample.png').write_bytes(b'\x89PNG\r\n\x1a\nexample')
    result=await tv.call('prepare_tv_display',{'content_type':'image','image_id':'sample.png'})
    answer(tv,result)
    await tv.call('confirm_tv_display',{'request_id':result['request_id']});await tv.task
    assert tv.sender.sent[0]['type']=='image' and tv.sender.sent[0]['text']==''


@pytest.mark.asyncio
async def test_generic_tool_is_advertised_and_routes_through_live_path():
    from lira.audio.doubao_realtime import TOOLS
    from test_conversation_paths import runtime
    names={tool['name'] for tool in TOOLS}
    assert {'prepare_tv_display','prepare_pinyin_practice','list_practice_textbooks'}<=names
    assert not {'prepare_tv_text','prepare_tv_image'} & names
    rt,paths=runtime();paths.tv=TvVoiceTools(sender=Sender())
    result=await paths._tool('prepare_tv_display',{'content_type':'text'})
    assert result['status']=='needs_input'
    result=await paths._tool('prepare_tv_display',{'content_type':'text','text':'基础功能'})
    paths._event('assistant_text',result['question']);paths._event('response_done','')
    paths._event('user_text','好的')
    await paths._tool('confirm_tv_display',{'request_id':result['request_id']});await paths.tv.task
    assert paths.tv.sender.sent[0]['text']=='基础功能'
    await paths.close()


@pytest.mark.asyncio
async def test_managed_clear_ownership_cancel_and_idempotence():
    from lira.realtime_capabilities import CapabilityRegistry, CapabilityExecutor
    class ClearSender(Sender):
        def __init__(self):super().__init__();self.clears=[]
        async def clear(self,content_id):self.clears.append(content_id);return {'cleared_id':content_id}
        async def probe(self):return {'version':'0.2','capabilities':['clear_content_id']}
    sender=ClearSender();tv=TvVoiceTools(sender=sender)
    registry=CapabilityRegistry();tv.register_capabilities(registry)
    probe=await tv._probe({})
    assert probe['status']=='ok' and probe['clear_supported'] and probe['version']=='0.2'
    assert 'clear_tv_content' in registry.entries
    from lira.tv_voice import TV_TOOLS
    assert 'clear_tv_content' not in {tool['name'] for tool in TV_TOOLS}
    assert (await tv._clear({'content_id':'invented'}))['status']=='failed'
    result=await prepare(tv)
    assert (await tv._clear({'content_id':result['request_id']}))['status']=='failed'
    answer(tv,result);await tv.call('confirm_tv_display',{'request_id':result['request_id']});await tv.task
    assert tv.result['content_id']==result['request_id']
    await tv.call('cancel_tv_display',{})
    assert sender.clears==[]
    executor=CapabilityExecutor(registry)
    cleared=await executor.call('clear_tv_content',{'content_id':result['request_id']},'clear-call')
    assert executor.metadata()[0]['status']=='completed'
    assert cleared=={'status':'ok','display_status':'cleared','cleared_id':result['request_id']}
    assert await tv._clear({'content_id':result['request_id']})==cleared
    assert sender.clears==[result['request_id']]
    await tv.close()
    assert (await tv._clear({'content_id':result['request_id']}))['status']=='failed'


@pytest.mark.asyncio
async def test_clear_replacement_and_receiver_failure_never_claim_cleared():
    class ClearSender(Sender):
        async def clear(self,content_id):raise RuntimeError('content mismatch')
    tv=TvVoiceTools(sender=ClearSender())
    ids=[]
    for _ in range(2):
        result=await prepare(tv);answer(tv,result)
        await tv.call('confirm_tv_display',{'request_id':result['request_id']});await tv.task
        ids.append(result['request_id'])
    assert (await tv._clear({'content_id':ids[0]}))['status']=='failed'
    assert (await tv._clear({'content_id':ids[1]}))['status']=='failed'
    assert tv.result['status']=='displayed'
    assert tv.displayed_id==ids[1]


@pytest.mark.asyncio
async def test_late_clear_ack_preserves_newer_display_ownership():
    entered,release=asyncio.Event(),asyncio.Event()
    class ClearSender(Sender):
        async def clear(self,content_id):
            entered.set();await release.wait();return {'cleared_id':content_id}
    tv=TvVoiceTools(sender=ClearSender())
    first=await prepare(tv);answer(tv,first)
    await tv.call('confirm_tv_display',{'request_id':first['request_id']});await tv.task
    clearing=asyncio.create_task(tv._clear({'content_id':first['request_id']}))
    await entered.wait()
    second=await prepare(tv);answer(tv,second)
    await tv.call('confirm_tv_display',{'request_id':second['request_id']});await tv.task
    release.set()
    assert (await clearing)['status']=='ok'
    assert tv.displayed_id==second['request_id']
    assert tv.result=={'status':'displayed','content_id':second['request_id']}


@pytest.mark.asyncio
@pytest.mark.parametrize('reply,accepted',[
    ('可以，请把拼音显示在电视上。',True),
    ('可以先把拼音显示在电视上。',True),
    ('你把拼音显示在电视上。',True),
    ('请把这页拼音显示在电视上。',True),
    ('你把拼音显示在电视上？',False),
    ('你把拼音显示在电视上吗？',False),
    ('你觉得可以把拼音显示在电视上吗？',False),
    ('好的，把这页投屏到电视上吧。',True),
    ('可以把拼音显示在电视上？',False),
    ('可以把拼音显示在电视上?',False),
    ('可以，不要把拼音显示在电视上。',False),
    ('可以解释为什么要把拼音显示在电视上吗？',False),
    ('可以的话再把拼音显示在电视上。',False),
    ('可以，但是先别把拼音显示在电视上。',False),
])
async def test_contextual_full_sentence_display_consent(tmp_path,reply,accepted):
    tools=TvVoiceTools(sender=Sender())
    prepared=await tools.call('prepare_tv_text',{'text':'dēng táng'})
    tools.event('assistant_text',prepared['question'])
    tools.event('response_done','')
    tools.event('user_text',reply)
    answer=await tools.call('confirm_tv_display',{'request_id':prepared['request_id']})
    assert (answer['status']=='displaying') is accepted
    await tools.close()
