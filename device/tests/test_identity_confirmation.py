import json
import time
import asyncio

import numpy as np
import pytest

from test_realtime_identity import cloud_identity, ordinary
from test_voice_enrollment import V, audio


async def candidate(paths,memory):
    mid=memory.register_voice('张三','张叔',np.array([.65,np.sqrt(1-.65**2)],np.float32))
    await ordinary(paths,audio())
    assert paths.speaker_input.context()['candidate']['score']==pytest.approx(.65)
    assert paths.speaker_input.context()['status']!='recognized'
    return mid


async def test_ordinary_reply_proactively_asks_and_saves_spoken_feedback(cloud_identity):
    _,paths,memory=cloud_identity
    asked=[]
    async def speak(text):
        asked.append(text)
        paths._event('assistant_prompt',text)
        return True
    paths.session.speak_text=speak
    await candidate(paths,memory)
    sid=paths.history.session_id
    paths._event('assistant_text','出去散步很不错。')
    paths._event('response_done','')
    await asyncio.sleep(.8)
    assert len(asked)==1 and '张三' in asked[0]
    assert paths.speaker_input.confirmation.pending['delivered']
    await ordinary(paths,audio(2),'是的')
    assert paths.speaker_input.context()['identity_source']=='user_confirmed'
    other=memory.people.create_member('李四')['id']
    await ordinary(paths,audio(3),'不是，我是李四')
    assert paths.speaker_input.member_id==other
    assert paths.speaker_input.context()['identity_source']=='user_corrected'
    assert paths.identity_journal.recent()['labels']=={'confirmed':1,'corrected':1}
    archive=paths.history.detail(sid)
    assert '出去散步' in archive['turns'][0]['assistant']
    assert '张三' in archive['turns'][0]['assistant']
    assert len(archive['turns'])==3
    assert not memory.voice_links(sid)


async def test_new_speech_cancels_queued_question_until_next_reply(cloud_identity):
    _,paths,memory=cloud_identity
    asked=[]
    async def speak(text):asked.append(text);return True
    paths.session.speak_text=speak
    await candidate(paths,memory)
    paths._event('response_done','')
    paths._event('input_started','')
    await asyncio.sleep(.8)
    assert not asked and not paths.speaker_input.confirmation.offered


async def test_failed_or_cancelled_proactive_audio_never_accepts_confirmation(cloud_identity):
    _,paths,memory=cloud_identity
    entered=asyncio.Event()
    async def speak(text):entered.set();await asyncio.sleep(10)
    paths.session.speak_text=speak
    await candidate(paths,memory)
    paths._event('response_done','')
    await asyncio.wait_for(entered.wait(),2)
    paths._event('input_started','')
    await ordinary(paths,audio(2),'是的')
    assert not paths.identity_journal.recent()['labels']
    assert paths.speaker_input.state!='recognized'
    assert not paths.speaker_input.confirmation.pending


async def test_unrelated_or_quoted_text_cannot_label_an_old_proactive_question(cloud_identity):
    _,paths,memory=cloud_identity
    async def speak(text):return True
    paths.session.speak_text=speak
    await candidate(paths,memory)
    paths._event('response_done','')
    await asyncio.sleep(.8)
    await ordinary(paths,audio(2),'如果我是李四呢？')
    assert not paths.identity_journal.recent()['labels']
    await ordinary(paths,audio(3),'是的')
    assert not paths.identity_journal.recent()['labels']
    assert paths.speaker_input.state!='recognized'


async def test_sustained_low_continuity_pauses_candidate_feedback(cloud_identity):
    rt,paths,memory=cloud_identity
    await candidate(paths,memory)
    s=paths.speaker_input
    request=await s.confirmation.request()
    rt.speaker_identity.embedding=lambda samples:(V if len(samples)>64000 else
        np.array([.61,np.sqrt(1-.61**2)],np.float32))
    # A retained healthy window prevents an immediate track replacement, while
    # two coherent low windows and a low weighted mean pause confirmation.
    s.continuity_scores.append((.7,4))
    await ordinary(paths,audio(2),'继续聊天')
    assert s.context().get('candidate')
    await ordinary(paths,audio(3),'是的')
    assert s.candidate_change_pending
    assert 'candidate' not in s.context()
    assert (await s.confirmation.feedback(dict(token=request['token'],decision='confirmed',name='')))['status']=='stale'


async def test_single_short_score_dip_keeps_candidate_and_confirmation_available(cloud_identity):
    rt,paths,memory=cloud_identity
    await candidate(paths,memory)
    s=paths.speaker_input
    s.continuity_scores.extend([(.79,4),(.78,4)])
    rt.speaker_identity.embedding=lambda samples:np.array([.6485,np.sqrt(1-.6485**2)],np.float32)
    await ordinary(paths,audio(2),'好的')
    assert s.snapshot()['suspect_probes']==1
    assert s.continuity_average()>.7
    assert s.context()['candidate']['name']=='张三'
    assert (await s.confirmation.request())['status']=='confirmation_requested'


async def test_confirmation_is_labelled_persistent_and_does_not_train(cloud_identity):
    rt,paths,memory=cloud_identity
    mid=await candidate(paths,memory)
    before=memory.voice_revision(mid)
    hint=await paths._tool('get_speaker_continuity_context',{})
    next_action=await paths._tool('report_speaker_continuity',dict(token=hint['token'],assessment='same_person',evidence='conversation_flow'))
    assert next_action['status']=='accepted'
    assert paths.speaker_input.confirmation.pending is None
    request=await paths._tool('request_identity_confirmation',{})
    args=dict(token=request['token'],decision='confirmed',name='')
    assert (await paths._tool('submit_identity_feedback',args))['status']=='stale'
    await ordinary(paths,audio(2),'今天天气不错')
    assert (await paths._tool('submit_identity_feedback',args))['status']=='needs_explicit_feedback'
    await ordinary(paths,audio(3),'是的，我是张三')
    result=await paths._tool('submit_identity_feedback',args)
    assert result['feedback_saved'] and result['identity_source']=='user_confirmed'
    assert result['name']=='张三' and not result['template_updated']
    assert memory.voice_revision(mid)==before
    assert (await paths._tool('submit_identity_feedback',args))['status']=='stale'
    data=paths.identity_journal.recent()
    assert data['labels']=={'confirmed':1}
    assert next(e for e in data['events'] if e['source']=='conversation')['details']['threshold']==.8
    assert not any('vector' in e for e in data['events'])


async def test_reject_does_not_reoffer_and_correction_is_separate_source(cloud_identity):
    _,paths,memory=cloud_identity
    await candidate(paths,memory)
    other=memory.register_voice('李四','李叔',np.array([0.,1.],np.float32))
    request=await paths._tool('request_identity_confirmation',{})
    await ordinary(paths,audio(2),'不对，我是李四')
    result=await paths._tool('submit_identity_feedback',dict(token=request['token'],decision='corrected',name='李四'))
    assert result['identity_source']=='user_corrected' and result['name']=='李四'
    assert paths.speaker_input.member_id==other
    await ordinary(paths,audio(3),'不是，我不是李四，我叫新访客')
    result=await paths._tool('request_identity_confirmation',{})
    assert result['status']=='unrecognized' and result['feedback_saved']
    assert 'candidate' not in result
    assert (await paths._tool('request_identity_confirmation',{}))['status']=='no_candidate'
    assert len(memory.people.list_members())==2


async def test_explicit_correction_tool_completes_without_reasking_old_name(cloud_identity):
    _,paths,memory=cloud_identity
    original=memory.register_voice('张三','张叔',V)
    other=memory.people.create_member('李四')['id']
    await ordinary(paths,audio(),'你好')
    assert paths.speaker_input.member_id==original
    await ordinary(paths,audio(2),'不对，你认错了，我不是张三，我是李四，请纠正。')
    result=await paths._tool('request_identity_confirmation',{})
    assert result['name']=='李四' and result['identity_source']=='user_corrected'
    assert result['feedback_saved'] and 'question' not in result
    assert paths.speaker_input.member_id==other
    assert paths.identity_journal.recent()['labels']=={'corrected':1}


async def test_candidate_reply_is_saved_even_if_model_reads_confirmation_tool_again(cloud_identity):
    _,paths,memory=cloud_identity
    await candidate(paths,memory)
    other=memory.people.create_member('李四')['id']
    await paths._tool('request_identity_confirmation',{})
    await ordinary(paths,audio(2),'不是，我是李四')
    result=await paths._tool('request_identity_confirmation',{})
    assert result['feedback_saved'] and result['name']=='李四'
    assert result['identity_source']=='user_corrected' and 'question' not in result
    assert paths.speaker_input.member_id==other


async def test_expiry_privacy_and_track_change_invalidate_feedback(cloud_identity):
    rt,paths,memory=cloud_identity
    await candidate(paths,memory)
    request=await paths._tool('request_identity_confirmation',{})
    await ordinary(paths,audio(2),'是的')
    paths.speaker_input.confirmation.pending['expires']=time.monotonic()-1
    args=dict(token=request['token'],decision='confirmed',name='')
    assert (await paths._tool('submit_identity_feedback',args))['status']=='stale'
    assert not paths.identity_journal.recent()['labels']
    rt.privacy._on=True
    assert (await paths.speaker_input.confirmation.feedback(args))['status']=='stale'
    assert not paths.identity_journal.recent()['labels']


async def test_rejection_prevents_candidate_reuse_and_never_reads_member_memory(cloud_identity):
    _,paths,memory=cloud_identity
    await candidate(paths,memory)
    request=await paths._tool('request_identity_confirmation',{})
    await ordinary(paths,audio(2),'不是，你认错人了')
    result=await paths._tool('submit_identity_feedback',dict(token=request['token'],decision='rejected',name=''))
    assert result['feedback_saved'] and result['status']=='unrecognized'
    assert paths.speaker_input.member_id is None
    assert 'candidate' not in result
    assert (await paths._tool('request_identity_confirmation',{}))['status']=='no_candidate'
    assert 'personal_memory' not in await paths._tool('get_personal_memory',{})
    assert paths.identity_journal.recent()['labels']=={'rejected':1}


async def test_multiple_low_score_candidates_are_not_automatically_identified(cloud_identity):
    _,paths,memory=cloud_identity
    await candidate(paths,memory)
    memory.register_voice('李四','李叔',np.array([.64,-np.sqrt(1-.64**2)],np.float32))
    await ordinary(paths,audio(2))
    ctx=paths.speaker_input.context()
    assert ctx['candidate']['alternatives']==2
    assert ctx['status']!='recognized'
    assert paths.identity_journal.recent()['events']


async def test_anonymous_memory_falls_back_to_previous_real_device_session(cloud_identity):
    _,paths,memory=cloud_identity
    h=paths.history
    h.event('user_text','上次我种了番茄');h.event('assistant_text','下次聊浇水');h.event('response_done');h.end()
    previous=h.sessions()[0]['id']
    h.begin();h.event('user_text','本轮不算上一次');h.event('assistant_text','当前回答')
    result=await paths._tool('get_personal_memory',{})
    fallback=result['device_recent_conversation']
    assert fallback['session_id']==previous
    assert fallback['ownership']=='unverified_device_history'
    assert fallback['is_current_user_memory'] is False
    assert '番茄' in json.dumps(fallback,ensure_ascii=False)
    assert 'personal_memory' not in result
    assert not memory.people.list_members()


def test_journal_api_feedback_auth_privacy_and_selected_profile(tmp_path):
    from test_dev_console import make_voice_memory_client,login
    from lira.identity_journal import IdentityJournal
    from lira.personal_memory import SpeakerIdentity
    _,client,rt=make_voice_memory_client(tmp_path)
    memory=rt.personal_memory
    mid=memory.register_voice('测试成员','朋友',V)
    identity=SpeakerIdentity(memory,None,extractor=object())
    journal=IdentityJournal(rt.store._conn)
    event=journal.record(identity,V,identity.match_diagnostics(V),source='profile_test',selected_member_id=mid)
    url='/dev/api/identity-matches'
    assert client.get(url).status_code==401
    login(client)
    assert client.get(url).json()['events'][0]['id']==event
    payload=dict(confirm=True,event_id=event,decision='confirmed',member_id=mid)
    assert client.post(url,json={**payload,'confirm':False}).status_code==400
    assert client.post(url,json=payload).json()['template_updated'] is False
    assert client.get(url).json()['labels']=={'confirmed':1}
    rt.privacy._on=True
    assert client.get(url).status_code==403
    assert client.post(url,json=payload).status_code==403


async def test_device_history_reports_manual_owner_without_claiming_current_user(cloud_identity):
    _,paths,memory=cloud_identity
    h=paths.history
    h.event('user_text','想去看大海');h.event('assistant_text','海边散步很好');h.end()
    sid=h.sessions()[0]['id']
    mid=memory.people.create_member('另一位成员')['id']
    memory.link_voice_turns(sid,h.detail(sid)['turns'],mid)
    result=await paths._tool('get_recent_device_conversation',{})
    assert result['ownership']=='unverified_device_history'
    assert result['is_current_user_memory'] is False
    assert result['turns'][0]['recorded_owner']['member_id']==mid
    assert paths.speaker_input.member_id is None


def test_labelled_evidence_survives_restart_and_unlabelled_retention(tmp_path):
    from lira.appliances.store import ApplianceStore
    from lira.identity_journal import IdentityJournal
    from lira.personal_memory import PersonalMemory,SpeakerIdentity
    path=tmp_path/'journal.db';store=ApplianceStore(path)
    memory=PersonalMemory(path,None,'unused',lambda:False)
    identity=SpeakerIdentity(memory,None,extractor=object())
    journal=IdentityJournal(store._conn)
    event=journal.record(identity,V,{},source='profile_test')
    journal.feedback(event,'rejected',None,None,'console_user')
    with store._conn:
        store._conn.executemany('INSERT INTO identity_match_events VALUES(?,?,?,?,?,?,?,?,?)',
            [(str(i),time.time(),'continuity',None,0,None,'test','{}','[]') for i in range(5001)])
    journal.record(identity,V,{},source='profile_test')
    store.close()
    store=ApplianceStore(path);journal=IdentityJournal(store._conn)
    assert journal.recent()['labels']=={'rejected':1}
    assert store._conn.execute('SELECT COUNT(*) FROM identity_match_events').fetchone()[0]==5001
    store.close()
