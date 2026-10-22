import json
import sqlite3
import pytest
from lira.dictation_service import DictationService
from lira.textbook_practice import TextbookPractice

@pytest.fixture
def service(tmp_path):
    folder=tmp_path/'books';folder.mkdir()
    (folder/'b.json').write_text(json.dumps(dict(id='b',title='语文',lessons=[dict(unit=1,number=2,title='课',two=['登'],four=['依','尽'],expansions='依靠、尽头、登高',text='原文')]),ensure_ascii=False),encoding='utf8')
    return DictationService(sqlite3.connect(':memory:'),TextbookPractice(folder),tmp_path/'cache',synthesize=lambda text,speed:b'\x01\x00'*240,member_exists=lambda n:n==1)

@pytest.mark.asyncio
async def test_model_order_word_recognition_and_idempotence(service):
    kw=dict(owner_id=1,session_id='s',paired_device_id='tv',callback_registration_id='cb')
    args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠'],'reason':'覆盖本课'}})
    result=await service.prepare_task(args,**kw)
    assert result['status']=='ready'
    task=result['task']
    assert [i['text'] for i in task['phases'][1]['items']]==['尽头','依靠']
    assert task['phases'][0]['items'][0]['text']=='登高'
    assert task['phases'][0]['items'][0]['audio_asset_id'] is None
    assert (await service.prepare_task({},**kw))['task']==task

@pytest.mark.asyncio
async def test_missing_coverage_and_unconfirmed_words(service):
    kw=dict(owner_id=1,session_id='s',paired_device_id='tv',callback_registration_id='cb')
    args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['依靠']}})
    assert (await service.prepare_task(args,**kw))['status']=='word_coverage_required'
    args['proposals']['2']['words']=['依旧','尽头']
    assert (await service.prepare_task(args,**kw))['status']=='word_confirmation_required'

@pytest.mark.asyncio
async def test_model_cannot_confirm_its_own_unknown_words(service):
    kw=dict(owner_id=1,session_id='approval',paired_device_id='tv',callback_registration_id='cb')
    args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['依旧','尽头'],'confirmed':True}})
    pending=await service.prepare_task(args,**kw)
    assert pending['status']=='word_confirmation_required'
    service.confirm_candidates(1,'approval',pending['confirmation_id'],turn='parent-click-1')
    assert (await service.prepare_task(args,**kw))['status']=='ready'

@pytest.mark.asyncio
async def test_candidate_approval_is_source_session_and_word_order_bound(service):
    kw=dict(owner_id=1,session_id='candidate',paired_device_id='tv',callback_registration_id='cb')
    args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['依旧','尽头'],'confirmed':True}})
    pending=await service.prepare_task(args,**kw)
    with pytest.raises(ValueError):service.confirm_candidates(1,'other',pending['confirmation_id'],turn='click')
    service.observe_initiator(1,'candidate','确认采用',turn='actual-reply')
    args['proposals']['2']['words']=['尽头','依旧']
    changed=await service.prepare_task(args,**kw)
    assert changed['status']=='word_confirmation_required'
    service.confirm_candidates(1,'candidate',changed['confirmation_id'],turn='click-two')
    assert (await service.prepare_task(args,**kw))['status']=='ready'

def test_owner_and_pinned_skill(service):
    args=dict(scope_type='lesson',start=2,owner_id=999)
    first=service.get_context(args,owner_id=1,session_id='s')
    service.skills.save('new instruction',expected_revision=first['skill']['revision'])
    assert service.get_context(args,owner_id=1,session_id='s')['skill']==first['skill']
    assert service.get_context(args,owner_id=1,session_id='new')['skill']['content']=='new instruction'
    with pytest.raises(ValueError):service.get_context(args,owner_id=2,session_id='s')

@pytest.mark.asyncio
async def test_audio_absence_and_privacy(service):
    service.synthesize=None
    kw=dict(owner_id=1,session_id='silent',paired_device_id='tv',callback_registration_id='cb')
    args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}})
    assert (await service.prepare_task(args,**kw))['status']=='audio_unavailable'
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0]==0
    service.privacy=lambda:True
    with pytest.raises(ValueError):await service.prepare_task(args,**kw)

@pytest.mark.asyncio
async def test_context_keeps_old_related_review_beyond_hundred_tasks(service):
    from lira.dictation_task_contract import content_hash
    import copy
    kw=dict(owner_id=1,session_id='first',paired_device_id='tv',callback_registration_id='cb')
    result=await service.prepare_task(dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}}),**kw)
    task=result['task'];iid=task['phases'][1]['items'][0]['item_id']
    service.results.review(1,task['task_id'],status='reviewed_with_errors',errors=[dict(item_id=iid,wrong_characters=['尽'])],expected_revision=0,request_id='review')
    service.results.record_assistance(1,task['task_id'],iid,'尽头的尽','turn')
    for n in range(101):
        other=copy.deepcopy(task);other['task_id']='other'+str(n)
        other['lessons'][0]['source']['number']=3
        other['content_hash']=content_hash(other)
        service.repository.submit(other,owner_id=1,paired_device_id='tv')
    context=service.get_context(dict(scope_type='lesson',start=2),owner_id=1,session_id='after')
    facts=context['lessons'][0]['facts']
    assert len(facts['reviews'])==1
    assert facts['reviews'][0]['errors'][0]['text']=='尽头'
    assert facts['assistance'][0]['text']=='尽头的尽'


@pytest.mark.asyncio
async def test_recognition_words_cover_two_targets_and_need_real_candidate_approval(service):
    kw=dict(owner_id=1,session_id='recognition-words',paired_device_id='tv',callback_registration_id='cb')
    args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠'],'recognition_words':['登山']}})
    pending=await service.prepare_task(args,**kw)
    assert pending['status']=='word_confirmation_required'
    assert pending['candidates']==['登山']
    service.confirm_candidates(1,kw['session_id'],pending['confirmation_id'],turn='actual-parent')
    result=await service.prepare_task(args,**kw)
    assert result['status']=='ready'
    item=result['task']['phases'][0]['items'][0]
    assert item['text']=='登山' and item['covers']==['登']
    assert item['audio_asset_id'] is None

@pytest.mark.asyncio
async def test_cloud_settings_pinned_and_local_not_called(service):
    import copy
    from lira.dictation_cloud_tts import DEFAULT_TTS
    config=copy.deepcopy(service.settings.get_v2()['parameters']);config['tts']=copy.deepcopy(DEFAULT_TTS);config['playback'].update(manual_navigation=True,repeat_count=1)
    service.settings.save_v2(config,expected_revision=1)
    service.get_context(dict(scope_type='lesson',start=2),owner_id=1,session_id='cloud')
    changed=copy.deepcopy(config);changed['tts']['speech_rate']=-30
    service.settings.save_v2(changed,expected_revision=2)
    calls=[]
    async def cloud(text,settings,allowed):
        assert allowed();calls.append((text,settings));return b'\x01\x00'*240
    service.cloud_synthesize=cloud
    def forbidden(*args):raise AssertionError('local synthesis fallback forbidden')
    service.synthesize=forbidden
    result=await service.prepare_task(dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}}),owner_id=1,session_id='cloud',paired_device_id='tv',callback_registration_id='cb')
    assert result['status']=='ready'
    assert len(calls)==2 and all(settings==DEFAULT_TTS for _,settings in calls)
    assert result['task']['config_revision']==2
    assert result['task']['playback']['manual_navigation'] is True

@pytest.mark.asyncio
async def test_cloud_failure_never_falls_back_or_creates_task(service):
    import copy
    from lira.dictation_cloud_tts import DEFAULT_TTS
    config=copy.deepcopy(service.settings.get_v2()['parameters']);config['tts']=copy.deepcopy(DEFAULT_TTS)
    service.settings.save_v2(config,expected_revision=1)
    async def fail(*args,**kwargs):raise ValueError('豆包语音资源不完整')
    service.cloud_synthesize=fail
    service.synthesize=lambda *args:(_ for _ in ()).throw(AssertionError('must not fallback'))
    result=await service.prepare_task(dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}}),owner_id=1,session_id='cloud-failed',paired_device_id='tv',callback_registration_id='cb')
    assert result['status']!='ready'
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0]==0
