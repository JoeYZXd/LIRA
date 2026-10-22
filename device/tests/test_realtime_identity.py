import asyncio
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from lira.personal_memory import SpeakerIdentity
from test_conversation_paths import runtime, FakeSession
from test_voice_enrollment import setup, audio, V


class Session(FakeSession):
    def __init__(self):
        super().__init__()
        self.contexts=[]
    async def set_identity_context(self, context):
        self.contexts.append(dict(context))


@pytest.fixture
async def cloud_identity(tmp_path):
    rt, paths=runtime()
    memory, local=setup(tmp_path,lambda:rt.privacy.is_on)
    local.cancel()
    rt.personal_memory,rt.voice_enrollment=memory,local
    identity=SpeakerIdentity(memory,None,extractor=object())
    identity.embedding=lambda samples:V
    actual_match=identity.match
    calls=[]
    def match(vector):
        calls.append(1)
        return actual_match(vector)
    identity.match=match
    rt.speaker_identity=identity
    rt.match_calls=calls
    paths.mode,paths.state,paths.session='doubao_realtime','listening',Session()
    yield rt,paths,memory
    await paths.close()
    await memory.close()
    rt.store.close()


def utterance(paths, samples, text='今天天气不错，我准备出去散步'):
    paths.feed(samples)
    paths._event('user_text',text)


async def ordinary(paths,samples,text='今天天气不错，我准备出去散步'):
    utterance(paths,samples,text)
    await paths.speaker_input._worker


async def test_candidate_moderate_continuity_keeps_accumulating_without_automatic_identity(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('测试成员','朋友',np.array([.635,np.sqrt(1-.635**2)],np.float32))
    await ordinary(paths,audio())
    rt.speaker_identity.embedding=lambda samples: (np.array([.67,np.sqrt(1-.67**2)],np.float32)
        if len(samples)<=64000 else V)
    await ordinary(paths,audio(2))
    state=paths.speaker_input.snapshot()
    assert state['search_attempts']==2
    assert state['suspect_probes']==0
    assert paths.speaker_input.context()['candidate']['score']==pytest.approx(.635)
    assert paths.speaker_input.context()['status']!='recognized'
    request=await paths._tool('request_identity_confirmation',{})
    assert request['status']=='confirmation_requested'


async def test_before_candidate_low_short_anchor_cannot_stall_raw_audio_discovery(cloud_identity):
    rt,paths,memory=cloud_identity
    profile=np.array([.4711,np.sqrt(1-.4711**2),0],np.float32)
    memory.register_voice('测试成员','朋友',profile)
    first=np.array([1.,0.,0.],np.float32)
    later=np.array([.5633,0.,np.sqrt(1-.5633**2)],np.float32)
    query=.65*profile+np.sqrt(1-.65**2)*np.cross(profile,later)/np.linalg.norm(np.cross(profile,later))
    values=iter([first,later,query])
    rt.speaker_identity.embedding=lambda samples:next(values)
    await ordinary(paths,audio()[:42240])
    assert paths.speaker_input.last_match['top_score']==pytest.approx(.4711)
    assert not paths.speaker_input.confirmation.candidate
    await ordinary(paths,audio(2))
    s=paths.speaker_input
    assert s.attempts==2 and s.seconds==pytest.approx(6.64)
    assert s.last_match['discovery_continuity_average']==pytest.approx(.5633)
    assert s.continuity_average() is None
    assert s.last_match['query_method']=='concatenated_audio'
    assert s.context()['candidate']['name']=='测试成员'
    assert s.state!='recognized' and not s.challenger


async def test_discovery_drift_is_bounded_and_cannot_automatically_adopt_inconsistent_voice(cloud_identity):
    rt,paths,memory=cloud_identity
    profile=np.array([1.,0.,0.],np.float32)
    memory.register_voice('测试成员','朋友',profile)
    values=iter([np.array([0.,1.,0.],np.float32),np.array([0.,0.,1.],np.float32),profile])
    rt.speaker_identity.embedding=lambda samples:next(values)
    await ordinary(paths,audio())
    await ordinary(paths,audio(2))
    s=paths.speaker_input
    assert s.attempts==2
    assert s.state!='recognized'
    assert s.last_match['reason']=='inconsistent_live_voice'
    assert s.last_match['threshold']==.8
    assert s.context()['candidate']['confirmed'] is False


async def test_turn_diagnostics_remain_on_archive_turn_after_identity_reset(cloud_identity):
    _,paths,memory=cloud_identity
    memory.register_voice('测试成员','朋友',V)
    await ordinary(paths,audio(),'第一句话')
    paths.speaker_input.clear()
    await ordinary(paths,audio(2),'第二句话')
    data=paths.history.detail(paths.history.session_id)
    assert len(data['turns'])==2
    for expected,turn in enumerate(data['turns'],1):
        progress=[e for e in turn['diagnostics'] if e['kind']=='voiceprint.progress']
        assert len(progress)==1 and progress[0]['turn_number']==expected
        assert progress[0]['payload']['process']['processed_turns']==expected


async def test_each_profile_lookup_logs_scores_even_if_identity_stays_collecting(cloud_identity,monkeypatch):
    import lira.conversation_paths as module
    _,paths,memory=cloud_identity
    events=[]
    monkeypatch.setattr(module,'safe_add_event',lambda telemetry,kind,text:events.append((kind,text)))
    memory.register_voice('测试成员','测试称呼',np.array([.75,np.sqrt(1-.75**2)],np.float32))
    for seed in range(3):await ordinary(paths,audio(seed))
    matches=[text for kind,text in events if kind=='realtime_identity_match']
    assert len(matches)==3
    assert all('0.7500' in text and '0.8000' in text and '最高分未达门槛' in text for text in matches)
    assert all('测试成员' not in text and '测试称呼' not in text for text in matches)
    assert paths.speaker_input.snapshot()['last_match']['reason']=='below_threshold'


async def test_profile_lookup_uses_original_audio_instead_of_averaging_short_window_vectors(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    calls=[]
    vectors=[np.array([s,np.sqrt(1-s*s)],np.float32) for s in (.65,.75,.85)]
    def embedding(samples):
        calls.append(len(samples))
        return vectors[min(len(calls)-1,2)]
    rt.speaker_identity.embedding=embedding
    await ordinary(paths,audio())
    assert paths.speaker_input.context()['status']=='collecting'
    assert paths.speaker_input.last_match['query_method']=='single_window'
    await ordinary(paths,audio(2))
    detail=paths.speaker_input.snapshot()
    assert detail['status']=='recognized' and detail['search_attempts']==2
    assert detail['last_match']['top_score']==pytest.approx(.85)
    assert detail['last_match']['threshold']==.8
    assert detail['last_match']['query_method']=='concatenated_audio'
    assert paths.speaker_input.context()['name']=='张三'
    assert calls==[64000,64000,128000]
    assert len(rt.match_calls)==2  # One query per attempt, no best-of-two selection.
    average=vectors[0]+vectors[1];average/=np.linalg.norm(average)
    assert average[0]<.8  # The previous query representation would reject.


async def test_original_audio_query_does_not_fall_back_to_a_higher_averaged_score(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('测试成员','测试称呼',np.array([1.,0.,0.],np.float32))
    vectors=[np.array(v,np.float32) for v in
             [(.79,np.sqrt(1-.79**2),0),(.79,.4,np.sqrt(1-.79**2-.4**2)),
              (.78,np.sqrt(1-.78**2),0)]]
    calls=[]
    def embedding(samples):
        calls.append(1)
        return vectors[min(len(calls)-1,2)]
    rt.speaker_identity.embedding=embedding
    await ordinary(paths,audio())
    await ordinary(paths,audio(2))
    average=vectors[0]+vectors[1];average/=np.linalg.norm(average)
    assert average[0]>.8
    detail=paths.speaker_input.snapshot()
    assert detail['last_match']['top_score']==pytest.approx(.78)
    assert detail['last_match']['reason']=='below_threshold'
    assert detail['status']=='collecting' and 'name' not in paths.speaker_input.context()
    assert len(rt.match_calls)==2


async def test_one_long_utterance_does_not_exhaust_identity_before_a_second_window(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('测试成员','测试称呼',np.array([0.,1.],np.float32))
    long_audio=np.tile(audio(),4)[:15*16000]
    await ordinary(paths,long_audio)
    assert paths.speaker_input.context()['status']=='collecting'
    assert paths.speaker_input.attempts==1
    assert paths.speaker_input.last_match['query_method']=='concatenated_audio'
    assert paths.speaker_input.last_match['query_speech_seconds']==10
    await ordinary(paths,audio(2))
    assert paths.speaker_input.context()['status']=='unrecognized'
    assert paths.speaker_input.attempts==2 and len(rt.match_calls)==2


async def test_each_turn_reports_identity_progress_even_when_cloud_context_is_unchanged(cloud_identity,monkeypatch):
    import lira.conversation_paths as module
    rt,paths,_=cloud_identity
    events=[]
    monkeypatch.setattr(module,'safe_add_event',lambda telemetry,kind,text:events.append((kind,text)))
    updates=[]
    async def update(context):
        changed=not updates or updates[-1]!=context['status']
        updates.append(context['status'])
        return changed
    paths.session.set_identity_context=update
    for seed in range(3):await ordinary(paths,audio(seed)[:6400])
    progress=[text for kind,text in events if kind=='realtime_identity_progress']
    assert len(progress)==3
    assert '第 3 轮' in progress[-1] and '短句累计中' in progress[-1]
    assert paths.speaker_input.snapshot()['pooled_speech_seconds']==1.2
    assert len(rt.match_calls)==0
    assert len([1 for kind,_ in events if kind=='realtime_identity'])==1


async def test_identity_progress_explains_missing_audio_and_clipping(cloud_identity):
    _,paths,_=cloud_identity
    paths._event('user_text','你知道我是谁吗')
    await paths.speaker_input._worker
    detail=paths.speaker_input.snapshot()
    assert detail['last_result']=='本轮没有可用音频'
    assert detail['last_audio_seconds']==0 and detail['processed_turns']==1
    await ordinary(paths,np.ones(64000,np.float32))
    detail=paths.speaker_input.snapshot()
    assert detail['last_result']=='削波过多，本轮未参与匹配'
    assert detail['feature_extractions']==0 and detail['search_attempts']==0


async def test_pre_candidate_fluctuation_continues_budget_and_retains_raw_speech(cloud_identity):
    rt,paths,_=cloud_identity
    await ordinary(paths,audio())
    before=paths.speaker_input.snapshot()
    rt.speaker_identity.embedding=lambda samples:np.array([.68,np.sqrt(1-.68**2)],np.float32)
    await ordinary(paths,audio(2))
    suspect=paths.speaker_input.snapshot()
    assert suspect['continuity_state']=='stable'
    assert suspect['speech_seconds']==before['speech_seconds']+4==8
    assert suspect['search_attempts']==before['search_attempts']+1==2
    rt.speaker_identity.embedding=lambda samples:V
    await ordinary(paths,audio(3))
    recovered=paths.speaker_input.snapshot()
    assert recovered['continuity_state']=='stable'
    assert recovered['speech_seconds']>=before['speech_seconds']+4
    assert recovered['speech_seconds']==12
    assert recovered['last_match']['query_method']=='concatenated_audio'
    assert recovered['status']=='unrecognized'  # All compatible speech reaches the 10s budget.
    assert recovered['cached_audio_seconds']<=30


async def test_one_different_probe_keeps_confirmed_identity_and_old_track(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    await ordinary(paths,audio())
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    await ordinary(paths,audio(2))
    assert paths.speaker_input.snapshot()['continuity_state']=='suspected_change'
    assert paths.speaker_input.seconds==4 and paths.speaker_input.attempts==1
    assert paths.speaker_input.context()['name']=='张三'
    rt.speaker_identity.embedding=lambda samples:V
    await ordinary(paths,audio(3))
    assert paths.speaker_input.context()['name']=='张三'
    assert len(rt.match_calls)==1


async def test_recognized_identity_survives_reported_sentence_fluctuations_and_stable_point7(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    await ordinary(paths,audio())
    for i,score in enumerate([.5717,.7833,.7293,.7841,.7285,.7453,.6385]+[.7]*8):
        vector=np.array([score,np.sqrt(1-score*score)],np.float32)
        rt.speaker_identity.embedding=lambda samples,v=vector:v
        await ordinary(paths,audio(i+1))
        context=await paths._tool('identify_current_speaker',{})
        assert context['status']=='recognized' and context['name']=='张三'
    assert paths.speaker_input.continuity_average()==pytest.approx(.7)
    assert len(rt.match_calls)==1


async def test_context_change_hint_requires_sustained_local_evidence(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    other=np.array([.62,np.sqrt(1-.62**2)],np.float32)
    memory.register_voice('李四','小李',other)
    await ordinary(paths,audio())
    ctx=await paths._tool('get_speaker_continuity_context',{})
    result=await paths._tool('report_speaker_continuity',{'token':ctx['token'],'assessment':'person_changed','evidence':'conversation_flow'})
    assert result['assessment']=='person_changed'
    assert paths.speaker_input.context()['name']=='张三'
    rt.speaker_identity.embedding=lambda samples:other
    await ordinary(paths,audio(2))
    assert paths.speaker_input.context()['name']=='张三'
    await ordinary(paths,audio(3))
    assert paths.speaker_input.context()['name']=='李四'


async def test_model_cannot_change_stable_point7_identity_or_read_other_memory(cloud_identity):
    rt,paths,memory=cloud_identity
    member=memory.register_voice('张三','张叔',V)
    await ordinary(paths,audio())
    for i in range(3):
        ctx=await paths._tool('get_speaker_continuity_context',{})
        await paths._tool('report_speaker_continuity',{'token':ctx['token'],'assessment':'person_changed','evidence':'conversation_flow'})
        rt.speaker_identity.embedding=lambda samples:np.array([.7,np.sqrt(.51)],np.float32)
        await ordinary(paths,audio(i+1))
    memory.link_voice_turns('test-session',[dict(id=1,user='下次聊花园',assistant='好的',created=1)],member)
    result=await paths._tool('get_personal_memory',{})
    assert result['name']=='张三' and '花园' in str(result['personal_memory'])
    assert len(rt.match_calls)==1


async def test_reported_7_88_seconds_are_not_reset_by_one_lower_scoring_probe(cloud_identity):
    rt,paths,_=cloud_identity
    await ordinary(paths,audio())
    await ordinary(paths,audio(2)[:62080])
    assert paths.speaker_input.seconds==pytest.approx(7.88)
    assert paths.speaker_input.attempts==2
    rt.speaker_identity.embedding=lambda samples:np.array([.68,np.sqrt(1-.68**2)],np.float32)
    await ordinary(paths,audio(3))
    assert paths.speaker_input.seconds==pytest.approx(11.88)
    assert paths.speaker_input.attempts==3 and len(rt.match_calls)==3
    assert paths.speaker_input.state=='unrecognized'
    assert paths.speaker_input.snapshot()['continuity_state']=='stable'


async def test_invalid_or_expired_assistance_cannot_change_local_evidence(cloud_identity):
    _,paths,_=cloud_identity
    await ordinary(paths,audio())
    context=await paths._tool('get_speaker_continuity_context',{})
    with pytest.raises(ValueError):
        await paths._tool('report_speaker_continuity',{
            'token':context['token'],'assessment':'force_merge','evidence':'conversation_flow'})
    paths.speaker_input.received_at-=31
    result=await paths._tool('report_speaker_continuity',{
        'token':context['token'],'assessment':'same_person','evidence':'conversation_flow'})
    assert result['status']=='stale'
    assert paths.speaker_input.seconds==4 and paths.speaker_input.attempts==1
    assert paths.speaker_input.assistance is None


async def test_extractor_failure_during_suspicion_is_reported_as_unavailable(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('测试成员','朋友',V)
    await ordinary(paths,audio())
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    await ordinary(paths,audio(2))
    def broken(samples):raise RuntimeError('extractor unavailable')
    rt.speaker_identity.embedding=broken
    for seed in range(3):await ordinary(paths,audio(seed+3))
    assert paths.speaker_input.context()['status']=='unavailable'
    assert paths.speaker_input.snapshot()['status']=='unavailable'
    assert paths.speaker_input.seconds==4


async def test_ordinary_chat_accumulates_short_turns_and_pushes_identity_without_tool_request(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    session=paths.session
    for seed in range(4):
        await ordinary(paths,audio(seed)[:9600])  # Four natural 0.6s utterances.
    assert paths.speaker_input.snapshot()['status']=='recognized'
    assert session.contexts[-1]['name']=='张三' and session.contexts[-1]['address']=='张叔'
    assert paths.speaker_input.utterance is None
    assert len(rt.match_calls)==1
    # A tiny identity question reads the established session identity, no test phrase.
    await ordinary(paths,audio(5)[:6400],'我是谁')
    result=await paths._tool('identify_current_speaker',{})
    assert result['status']=='recognized' and result['name']=='张三'
    assert len(rt.match_calls)==1 and not rt.voice_enrollment.active


async def test_unmatched_search_is_bounded_then_only_invites_when_asked(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    for seed in range(5):await ordinary(paths,audio(seed)[:32000])
    status=paths.speaker_input.snapshot()
    assert status['status']=='unrecognized' and status['speech_seconds']==10
    assert status['search_attempts']==3 and len(rt.match_calls)==3
    for seed in range(5,10):await ordinary(paths,audio(seed)[:32000])
    assert len(rt.match_calls)==3  # No repeated profile-bank lookup.
    assert 'name' not in paths.session.contexts[-1]
    await ordinary(paths,audio(11)[:6400],'你知道我是谁吗')
    result=await paths._tool('identify_current_speaker',{})
    assert result['status']=='unrecognized' and '只有用户询问身份' in result['instructions']
    assert not rt.voice_enrollment.active and len(memory.profiles())==1


async def test_known_identity_stops_repeated_lookup_but_detects_a_new_speaker(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    second=np.array([0.,1.],np.float32)
    memory.register_voice('李四','小李',second)
    await ordinary(paths,audio())
    for seed in range(1,5):await ordinary(paths,audio(seed))
    assert len(rt.match_calls)==1
    epoch = paths.speaker_input.epoch
    track = paths.speaker_input.track_generation
    rt.speaker_identity.embedding=lambda samples:second
    await ordinary(paths,audio(7))
    assert (await paths._tool('identify_current_speaker',{}))['status']=='recognized'
    assert paths.speaker_input.snapshot()['continuity_state']=='suspected_change'
    await ordinary(paths,audio(8))
    await ordinary(paths,audio(18))
    await ordinary(paths,audio(28))
    result=await paths._tool('identify_current_speaker',{})
    assert result['name']=='李四' and result['address']=='小李'
    assert len(rt.match_calls)==2
    assert paths.session.contexts[-1]['name']=='李四'
    assert paths.speaker_input.epoch == epoch
    assert paths.speaker_input.track_generation == track+1
    # The prior track can be restored after another multi-probe change.
    rt.speaker_identity.embedding=lambda samples:V
    await ordinary(paths,audio(9))
    assert paths.speaker_input.context()['name']=='李四'
    await ordinary(paths,audio(10))
    await ordinary(paths,audio(19))
    assert paths.speaker_input.context()['name']=='张三'
    assert paths.speaker_input.epoch == epoch
    assert paths.speaker_input.track_generation == track+2
    assert len(rt.match_calls)==2


async def test_assistance_is_fresh_advisory_and_cannot_merge_a_distinct_voice(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    await ordinary(paths,audio())
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    await ordinary(paths,audio(2))
    context=await paths._tool('get_speaker_continuity_context',{})
    assert 'name' not in context and 'vector' not in context
    report={'token':context['token'],'assessment':'same_person','evidence':'conversation_flow'}
    result=await paths._tool('report_speaker_continuity',report)
    assert result['status']=='accepted'
    assert (await paths._tool('report_speaker_continuity',report))['status']=='duplicate'
    await ordinary(paths,audio(3))
    assert paths.speaker_input.snapshot()['continuity_state']=='suspected_change'
    assert paths.speaker_input.context()['name']=='张三'
    assert paths.speaker_input.seconds==4 and len(rt.match_calls)==1
    assert (await paths._tool('report_speaker_continuity',report))['status']=='stale'
    await ordinary(paths,audio(4))
    assert paths.speaker_input.context()['status']=='unrecognized'
    assert paths.speaker_input.previous_track['member_id'] is not None
    assert len(rt.match_calls)==2


async def test_only_explicit_handover_hint_can_shorten_multi_probe_confirmation(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('测试成员','朋友',np.array([.65,np.sqrt(1-.65**2)],np.float32))
    await ordinary(paths,audio())
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    await ordinary(paths,audio(2)[:32000])
    context=await paths._tool('get_speaker_continuity_context',{})
    result=await paths._tool('report_speaker_continuity',{
        'token':context['token'],'assessment':'person_changed','evidence':'explicit_handover'})
    assert result['assessment']=='person_changed'
    await ordinary(paths,audio(3)[:32000])
    assert paths.speaker_input.snapshot()['continuity_state']=='stable'
    assert paths.speaker_input.previous_track['seconds']==4
    assert paths.speaker_input.seconds==4


async def test_cached_tracks_are_bounded_and_privacy_clears_audio_and_hints(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('测试成员','朋友',np.array([.65,np.sqrt(1-.65**2)],np.float32))
    long_audio=np.tile(audio(),4)[:15*16000]
    await ordinary(paths,long_audio)
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    await ordinary(paths,long_audio)
    await ordinary(paths,long_audio)
    rt.speaker_identity.embedding=lambda samples:np.array([-1.,0.],np.float32)
    await ordinary(paths,long_audio)
    assert paths.speaker_input.snapshot()['cached_audio_seconds']==30
    await rt.privacy.set_enabled(True,source='test')
    await paths.apply_privacy(True)
    assert paths.speaker_input.snapshot()['cached_audio_seconds']==0
    assert paths.speaker_input.previous_track is None and not paths.speaker_input.challenger
    with pytest.raises(PermissionError):await paths._tool('get_speaker_continuity_context',{})


async def test_recognized_chat_keeps_live_anchor_when_next_sentence_differs_from_enrollment(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    live=np.array([.82,np.sqrt(1-.82**2)],np.float32)
    next_sentence=np.array([.65,np.sqrt(1-.65**2)],np.float32)
    rt.speaker_identity.embedding=lambda samples:live
    await ordinary(paths,audio(),'我现在在无锡')
    assert paths.speaker_input.context()['status']=='recognized'
    # Same conversational voice, despite a lower score against the enrollment template.
    assert np.dot(live,next_sentence)>.95
    rt.speaker_identity.embedding=lambda samples:next_sentence
    await ordinary(paths,audio(2),'大概什么时候会下雨')
    await ordinary(paths,audio(3)[:6400],'你知道我是谁吗')
    result=await paths._tool('identify_current_speaker',{})
    assert result['status']=='recognized' and result['name']=='张三'
    assert len(rt.match_calls)==1
    # A single changed probe is held; sustained different speech clears identity.
    rt.speaker_identity.embedding=lambda samples:np.array([-1.,0.],np.float32)
    await ordinary(paths,audio(4))
    assert paths.speaker_input.context()['status']=='recognized'
    await ordinary(paths,audio(5))
    await ordinary(paths,audio(6))
    assert paths.speaker_input.context()['status']=='unrecognized'
    assert 'name' not in paths.speaker_input.context()


async def test_new_unknown_voice_never_inherits_identity_or_self_report(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    await ordinary(paths,audio())
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    await ordinary(paths,audio(2),'我是张三，今天打算去散步')
    assert paths.speaker_input.context()['status']=='recognized'
    await ordinary(paths,audio(3))
    await ordinary(paths,audio(4))
    result=await paths._tool('identify_current_speaker',{})
    assert result['status']=='unrecognized' and 'name' not in result
    assert 'name' not in paths.session.contexts[-1]
    assert len(memory.profiles())==1


async def test_noise_echo_and_ambiguous_profiles_do_not_establish_identity(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    memory.enroll('另一人',np.array([.97,np.sqrt(1-.97**2)],np.float32))
    paths.session.output_occupied=True
    paths.feed(audio())
    assert paths.speaker_input.frames==0
    paths._event('user_text','一段回声')
    await paths.speaker_input._worker
    paths.session.output_occupied=False
    for samples in [np.zeros(64000,np.float32),np.ones(64000,np.float32)]:
        await ordinary(paths,samples)
    assert len(rt.match_calls)==0
    await ordinary(paths,audio())
    assert (await paths._tool('identify_current_speaker',{}))['status']=='collecting'
    assert all('name' not in item for item in paths.session.contexts)


async def test_audio_is_bounded_and_mode_change_clears_session_identity(cloud_identity):
    _,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    for _ in range(20):paths.feed(audio())
    assert paths.speaker_input.frames==15*16000
    paths._event('user_text','较长讲话')
    assert len(paths.speaker_input.utterance)==15*16000
    await paths.speaker_input._worker
    await paths.switch('classic')
    assert paths.speaker_input.utterance is None and not paths.speaker_input.parts
    assert paths.speaker_input.context()['status']=='collecting'


async def test_privacy_during_background_extraction_drops_late_identity(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    entered,release=threading.Event(),threading.Event()
    def embedding(samples):
        entered.set();release.wait(5);return V
    rt.speaker_identity.embedding=embedding
    session=paths.session
    utterance(paths,audio())
    for _ in range(100):
        if entered.is_set():break
        await asyncio.sleep(.01)
    assert entered.is_set()
    await rt.privacy.set_enabled(True,source='test')
    await paths.apply_privacy(True)
    release.set()
    assert paths.speaker_input.utterance is None and not paths.speaker_input.jobs
    assert not any(item['status']=='recognized' for item in session.contexts)
    with pytest.raises(PermissionError):await paths._tool('identify_current_speaker',{})


async def test_successful_enrollment_unlocks_unknown_session_without_repeated_search(cloud_identity):
    rt,paths,memory=cloud_identity
    for seed in range(5):await ordinary(paths,audio(seed)[:32000])
    assert paths.speaker_input.context()['status']=='unrecognized'
    member=memory.register_voice('张三','张叔',V)
    paths.speaker_input.accept_enrolled_member(member)
    await paths._publish_identity(paths.speaker_input.context())
    await ordinary(paths,audio(9)[:6400],'我是谁')
    assert (await paths._tool('identify_current_speaker',{}))['name']=='张三'
    assert paths.speaker_input.snapshot()['search_attempts']==0
    assert len(rt.match_calls)==3


async def test_missing_model_and_bad_tool_args_never_guess(cloud_identity):
    rt,paths,_=cloud_identity
    rt.speaker_identity=None
    await ordinary(paths,audio())
    assert (await paths._tool('identify_current_speaker',{}))['status']=='unavailable'
    with pytest.raises(ValueError):await paths._tool('identify_current_speaker',{'name':'张三'})


async def test_identity_context_uses_session_update_and_removes_old_name():
    from lira.audio.doubao_realtime import RealtimeSession,RealtimeConfig
    sent=[]
    events=[]
    class Socket:
        async def send(self,message):sent.append(json.loads(message))
        async def close(self):pass
        remaining=2
        async def recv(self):
            if not self.remaining:raise asyncio.CancelledError
            self.remaining-=1
            return json.dumps({'type':'session.updated'})
    class Player:
        async def stop(self):pass
        async def close(self):pass
    session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *a:events.append(a),on_tool=None)
    session.ws=Socket()
    context={'status':'recognized','name':'张三','address':'张叔'}
    await session.set_identity_context(context)
    await session.set_identity_context(context)
    await session.set_identity_context({'status':'unrecognized'})
    assert len(sent)==2 and all(item['type']=='session.update' for item in sent)
    assert '张三' in sent[0]['session']['instructions']
    assert '张三' not in sent[1]['session']['instructions'] and '张叔' not in sent[1]['session']['instructions']
    assert 'unrecognized' in sent[1]['session']['instructions']
    assert '每一轮身份工具结果仅对该轮回复有效' in sent[0]['session']['instructions']
    assert '只能用“你”称呼' in sent[1]['session']['instructions']
    assert '覆盖历史对话和旧工具结果' in sent[1]['session']['instructions']
    with pytest.raises(asyncio.CancelledError):await session._receive()
    assert [e for e in events if e[0]=='identity_context']==[('identity_context','身份配置已收到接口确认；本地状态：已识别身份。模型回答前仍需读取本地身份工具。'),
                    ('identity_context','身份配置已收到接口确认；本地状态：足量声音仍未匹配登记声纹。模型回答前仍需读取本地身份工具。')]
    await session.close()

async def test_late_previous_turn_is_not_published_over_new_speaker(cloud_identity):
    rt,paths,memory=cloud_identity
    memory.register_voice('张三','张叔',V)
    entered,release=threading.Event(),threading.Event()
    calls=[]
    def embedding(samples):
        calls.append(1)
        if len(calls)==1:
            entered.set();release.wait(5);return V
        return np.array([0.,1.],np.float32)
    rt.speaker_identity.embedding=embedding
    utterance(paths,audio())
    for _ in range(100):
        if entered.is_set():break
        await asyncio.sleep(.01)
    assert entered.is_set()
    utterance(paths,audio(2),'现在换一个人说话')
    release.set()
    await paths.speaker_input._worker
    assert paths.speaker_input.context()['status']=='collecting'
    assert all('name' not in item for item in paths.session.contexts)


async def test_broken_extractor_stops_background_retries_until_new_session(cloud_identity):
    rt,paths,_=cloud_identity
    calls=[]
    def broken(samples):
        calls.append(1)
        raise RuntimeError('native extractor failed')
    rt.speaker_identity.embedding=broken
    for seed in range(6):await ordinary(paths,audio(seed))
    assert len(calls)==3 and paths.speaker_input.context()['status']=='unavailable'


async def test_native_extractor_is_serialized_between_registration_and_background(cloud_identity):
    import time
    _,_,memory=cloud_identity
    active=[]
    running=[0]
    class Extractor:
        def create_stream(self):
            return SimpleNamespace(accept_waveform=lambda *a:None,input_finished=lambda:None)
        def is_ready(self,stream):return True
        def compute(self,stream):
            running[0]+=1;active.append(running[0])
            time.sleep(.02)
            running[0]-=1
            return V
    identity=SpeakerIdentity(memory,None,extractor=Extractor())
    await asyncio.gather(asyncio.to_thread(identity.embedding,audio()),asyncio.to_thread(identity.embedding,audio(2)))
    assert max(active)==1
