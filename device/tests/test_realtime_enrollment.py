import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from test_conversation_paths import runtime, FakeSession
from test_voice_enrollment import setup, audio, V


@pytest.fixture
async def cloud(tmp_path):
    rt, paths = runtime()
    memory, legacy = setup(tmp_path, lambda:rt.privacy.is_on)
    legacy.cancel()
    rt.personal_memory, rt.voice_enrollment = memory, legacy
    rt.speaker_identity = SimpleNamespace(embedding=lambda samples:V)
    paths.mode, paths.state, paths.session = 'doubao_realtime', 'listening', FakeSession()
    await paths._tool('start_voice_enrollment', {})
    await paths._tool('set_voice_enrollment_details', {'name':'张三','address':'小张'})
    yield rt, paths, memory
    await paths.close()
    await memory.close()
    rt.store.close()


async def segment(paths, samples, text='我喜欢早晨去公园散步'):
    await paths._tool('start_voice_sample', {})
    paths.feed(samples)
    paths._event('user_text', text)
    return await paths._tool('check_voice_sample', {})


async def ready(paths):
    for seed in (1,2,3):
        result=await segment(paths,audio(seed))
    assert result['status']=='ready_to_confirm'


async def test_cloud_capture_excludes_playback_tail_and_waiting_silence(cloud):
    rt, paths, memory = cloud
    await paths._tool('start_voice_sample', {})
    paths.session.output_occupied=True
    paths.feed(audio())
    assert paths.enrollment.frames==0
    paths.session.output_occupied=False
    paths.feed(np.zeros(16000*40,np.float32))
    assert paths.enrollment.frames==0 and not paths.enrollment.overflow
    paths.feed(audio())
    paths._event('user_text','这是一段四秒的语音')
    result=await paths._tool('check_voice_sample', {})
    assert result['segments']==1 and result['speech_seconds']==4
    assert not memory.profiles() and not rt.voice_enrollment.active


async def test_quality_retry_duplicate_and_actual_speech_duration(cloud):
    rt, paths, _ = cloud
    for samples in [np.zeros(64000,np.float32), audio()[:4000], np.ones(64000,np.float32)]:
        assert (await segment(paths,samples))['status']=='retry'
    assert (await segment(paths,audio()))['segments']==1
    assert (await segment(paths,audio()))['status']=='retry'
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    assert (await segment(paths,audio(2)))['status']=='retry'
    assert paths.enrollment.snapshot()['segments']==1
    rt.speaker_identity.embedding=lambda samples:V
    samples=np.concatenate([audio(3)[:32000],np.zeros(16000*8,np.float32)])
    result=await segment(paths,samples)
    assert result['speech_seconds']==6  # trailing silence cannot satisfy readiness
    assert result['status']=='samples'


async def test_saving_requires_new_explicit_confirmation_same_speaker(cloud):
    rt, paths, memory=cloud
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='not_ready'
    await ready(paths)
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='confirmation_required'
    paths.feed(audio(4));paths._event('user_text','好的')
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='confirmation_required'
    paths.feed(audio(5));paths._event('user_text','我确认按这个姓名和称呼保存')
    rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='confirmation_required'
    assert not memory.profiles()
    rt.speaker_identity.embedding=lambda samples:V
    paths.feed(audio(6));paths._event('user_text','我确认按这个姓名和称呼保存')
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='saved'
    assert len(memory.profiles())==1
    assert not paths.enrollment.parts and paths.enrollment.candidate is None
    assert paths.session is not None and paths.state=='listening'


async def test_updated_details_invalidate_previous_confirmation(cloud):
    _, paths, memory=cloud
    await ready(paths)
    paths.feed(audio(4));paths._event('user_text','我确认按这个姓名和称呼保存')
    await paths._tool('set_voice_enrollment_details',{'name':'张三','address':'张叔'})
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='confirmation_required'
    paths.feed(audio(5));paths._event('user_text','我确认按这个姓名和称呼保存')
    result=await paths._tool('save_voice_enrollment',{})
    assert result['status']=='saved'
    assert memory.preferred_address(result['member_id'])=='张叔'


async def test_cancel_reset_expiry_and_mode_change_clear_audio(cloud):
    _, paths, memory=cloud
    await segment(paths,audio())
    result=await paths._tool('reset_voice_samples',{})
    assert result['segments']==0 and result['speech_seconds']==0
    await paths._tool('start_voice_sample',{})
    paths.feed(audio(2))
    paths._event('user_text','取消注册')
    assert paths.enrollment.snapshot()['status']=='idle'
    assert not paths.enrollment.parts and paths.enrollment.candidate is None
    assert paths.session is not None and not memory.profiles()
    await paths._tool('start_voice_enrollment',{})
    paths.enrollment.enrollment.started-=241
    assert (await paths._tool('get_voice_enrollment_status',{}))['status']=='idle'
    await paths._tool('start_voice_enrollment',{})
    await paths.switch('classic')
    assert paths.enrollment.snapshot()['status']=='idle' and paths.session is None


async def test_privacy_during_embedding_prevents_late_sample_or_save(cloud):
    rt, paths, memory=cloud
    entered,release=threading.Event(),threading.Event()
    def embedding(samples):
        entered.set()
        release.wait(5)
        return V
    rt.speaker_identity.embedding=embedding
    task=asyncio.create_task(segment(paths,audio()))
    for _ in range(100):
        if entered.is_set():break
        await asyncio.sleep(.01)
    assert entered.is_set()
    await rt.privacy.set_enabled(True,source='test')
    await paths.apply_privacy(True)
    release.set()
    assert (await task)['status']=='cancelled'
    assert not memory.profiles() and not paths.enrollment.parts
    with pytest.raises(PermissionError):
        await paths._tool('save_voice_enrollment',{})


async def test_overlong_capture_and_existing_identity_are_not_saved(cloud):
    _, paths, memory=cloud
    assert (await segment(paths,np.tile(audio(),8)))['status']=='retry'
    assert paths.enrollment.frames==0 and not memory.profiles()
    memory.register_voice('已有成员','阿姨',np.array([0.,1.],np.float32))
    result=await paths._tool('set_voice_enrollment_details',{'name':'已有成员','address':'小张'})
    assert result['status']=='name_exists'
    await ready(paths)
    # A different name cannot register an already enrolled voice either.
    memory.register_voice('另一个名字','小李',V)
    paths.feed(audio(4));paths._event('user_text','我确认按这个姓名和称呼保存')
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='not_saved'
    assert len(memory.profiles())==2

def test_long_confirmation_is_explicit_and_does_not_accept_ambiguous_consent():
    from lira.realtime_enrollment import LONG_CONFIRM, is_confirmation
    assert is_confirmation(LONG_CONFIRM+'。')
    assert is_confirmation('我确认按这个姓名和称呼保存。')
    for text in ['好的','不要保存','我没有核对姓名和称呼','请问如何确认保存','我不确认按这个姓名和称呼保存']:
        assert not is_confirmation(text)


async def test_long_confirmation_can_complete_verified_local_save(cloud):
    from lira.realtime_enrollment import LONG_CONFIRM
    _, paths, memory=cloud
    await ready(paths)
    paths.feed(audio(4))
    paths._event('user_text',LONG_CONFIRM+'。')
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='saved'
    assert len(memory.profiles())==1

async def test_confirmation_matches_final_profile_while_collection_keeps_pairwise_gate(cloud):
    rt, paths, memory=cloud
    def direction(angle):
        theta=np.deg2rad(angle)
        return np.array([np.cos(theta),np.sin(theta)],np.float32)
    for seed,angle in enumerate([-10,0,20],1):
        rt.speaker_identity.embedding=lambda samples, angle=angle:direction(angle)
        result=await segment(paths,audio(seed))
    assert result['status']=='ready_to_confirm'
    # A confirmation can match the final enrolled profile even when its score
    # against one individual sample is below the collection consistency gate.
    confirmation=direction(25)
    vectors=paths.enrollment.enrollment.vectors
    profile=np.mean(vectors,axis=0);profile/=np.linalg.norm(profile)
    assert min(float(np.dot(confirmation,v)) for v in vectors)<.85
    assert float(np.dot(confirmation,profile))>.85
    rt.speaker_identity.embedding=lambda samples:confirmation
    paths.feed(audio(4));paths._event('user_text','我确认按这个姓名和称呼保存')
    assert (await paths._tool('save_voice_enrollment',{}))['status']=='saved'
    assert len(memory.profiles())==1
