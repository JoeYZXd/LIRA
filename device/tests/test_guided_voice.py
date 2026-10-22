import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from lira.ui.guided_voice import GuidedVoice


def rig():
    saved=[]
    async def stop(): pass
    memory=SimpleNamespace(find_member=lambda name: None, profiles=lambda: [],
        people=SimpleNamespace(list_members=lambda: []),
        save_guided_voice=lambda *args, **kw: saved.append(args) or 7)
    params=SimpleNamespace(min_rms=.01, min_segment_seconds=1, max_clip_ratio=.05,
        consistency_threshold=.85, assign_threshold=.73, match_threshold=.8, match_margin=.1)
    vector=np.array([1.,0.],np.float32)
    rt=SimpleNamespace(privacy=SimpleNamespace(is_on=False), personal_memory=memory,
        speaker_identity=SimpleNamespace(embedding=lambda audio: vector.copy()),
        conversation_paths=SimpleNamespace(stop=stop), voice_enrollment=SimpleNamespace(params=params))
    handle=SimpleNamespace(runtime=rt,dev_mutex=asyncio.Lock(),recording_active=False)
    return GuidedVoice(handle), saved


async def read(g, seconds=9):
    await g.action('record')
    g.feed(np.ones(int(16000*seconds),np.float32)*.1)
    return await g.action('stop')


@pytest.mark.asyncio
async def test_independent_validation_required_and_not_in_template():
    g,saved=rig()
    await g.begin('测试','朋友')
    try:
        for _ in range(4): await read(g)
        assert not saved
        with pytest.raises(ValueError): await g.action('save')
        await read(g)
        assert g.validation['passed']
        assert len(g.vectors)==4 and len(g.clips)==4
        result=await g.action('save')
        assert result['saved'] and len(saved)==1
        assert not g.handle.dev_mutex.locked() and g.rt.guided_voice_capture is None
    finally: g.cancel()


@pytest.mark.asyncio
async def test_failed_validation_does_not_save_and_can_retry():
    g,saved=rig()
    await g.begin('测试','朋友')
    try:
        for _ in range(4): await read(g)
        g.rt.speaker_identity.embedding=lambda a: np.array([.7,np.sqrt(.51)],np.float32)
        await read(g)
        assert not g.validation['passed'] and not saved
        with pytest.raises(ValueError): await g.action('save')
        g.rt.speaker_identity.embedding=lambda a: np.array([1.,0.],np.float32)
        await read(g)
        assert g.validation['passed']
    finally: g.cancel()


@pytest.mark.asyncio
async def test_short_clip_privacy_and_cancel_release():
    g,saved=rig()
    await g.begin('测试','朋友')
    with pytest.raises(ValueError, match='不足'): await read(g,2)
    assert not g.clips
    await g.action('record')
    g.rt.privacy.is_on=True
    g.feed(np.ones(16000,np.float32))
    assert not g.parts and not g.recording
    await asyncio.sleep(.3)
    assert not g.token and not g.handle.dev_mutex.locked() and not saved


@pytest.mark.asyncio
async def test_other_profile_margin_and_capture_interrupt():
    g,saved=rig()
    await g.begin('测试','朋友')
    try:
        await g.action('record')
        g.feed(np.ones(16000,np.float32)*.1)
        g.interrupt()
        assert not g.parts and not g.recording
        for _ in range(4): await read(g)
        g.rt.personal_memory.profiles=lambda: [(1,np.array([.95,np.sqrt(1-.95**2)],np.float32))]
        await read(g)
        assert g.validation['score']==1 and not g.validation['passed']
        with pytest.raises(ValueError): await g.action('save')
        assert not saved
    finally: g.cancel()


@pytest.mark.asyncio
async def test_repeated_cancel_cannot_release_another_lock_and_timeout():
    g,_=rig()
    await g.begin('测试','朋友')
    g.deadline=0
    await asyncio.sleep(.3)
    assert not g.token and not g.handle.dev_mutex.locked()
    await g.handle.dev_mutex.acquire()
    g.cancel()
    assert g.handle.dev_mutex.locked()
    g.handle.dev_mutex.release()


@pytest.mark.asyncio
async def test_borderline_segment_is_retained_but_failed_heldout_cannot_save():
    g,saved=rig()
    await g.begin('测试','朋友')
    try:
        await read(g)
        await read(g)
        g.rt.speaker_identity.embedding=lambda a: np.array([.8,.6],np.float32)
        state=await read(g)
        assert state['step']==3 and state['segment_checks'][2]['status']=='borderline'
        assert '已保留' in state['message'] and not saved
        await read(g)
        before=g.template().copy()
        g.rt.speaker_identity.embedding=lambda a: np.array([0.,1.],np.float32)
        await read(g)
        assert not g.validation['passed']
        np.testing.assert_array_equal(before,g.template())
        with pytest.raises(ValueError): await g.action('save')
        assert not saved
        g.rt.speaker_identity.embedding=lambda a: before.copy()
        await read(g)
        assert g.validation['passed']
        await g.action('save')
        assert len(saved)==1
    finally: g.cancel()


@pytest.mark.asyncio
async def test_centroid_replaces_all_pairs_and_obvious_anomaly_is_rejected():
    g,_=rig()
    await g.begin('测试','朋友')
    try:
        await read(g)
        g.rt.speaker_identity.embedding=lambda a: np.array([.9,np.sqrt(.19)],np.float32)
        await read(g)
        vector=np.array([.95,-np.sqrt(1-.95**2)],np.float32)
        assert np.dot(vector,g.vectors[1]) < .73
        g.rt.speaker_identity.embedding=lambda a: vector.copy()
        state=await read(g)
        assert state['step']==3 and state['segment_checks'][2]['status']=='consistent'
        previous=g.template().copy()
        g.rt.speaker_identity.embedding=lambda a: -previous
        with pytest.raises(ValueError,match='综合模板'): await read(g)
        assert len(g.vectors)==3 and len(g.segment_checks)==3
        np.testing.assert_array_equal(g.template(),previous)
    finally: g.cancel()


@pytest.mark.asyncio
async def test_same_voice_conflict_resolve_reuses_heldout_and_atomically_replaces(tmp_path):
    from lira.personal_memory import PersonalMemory
    g,_=rig()
    m=PersonalMemory(tmp_path/'voice.db',None,'test',lambda:False)
    old=m.register_voice('原姓名','原称呼',np.array([1.,0.],np.float32))
    m.people.append_messages(old,[('user','保留的聊天')])
    old_bytes=m.db.execute('SELECT vector FROM voice_profiles').fetchone()[0]
    g.rt.personal_memory=m
    g.rt.speaker_identity.embedding=lambda a: np.array([.95,np.sqrt(1-.95**2)],np.float32)
    await g.begin('新姓名','新称呼')
    try:
        for _ in range(5): await read(g)
        assert not g.validation['passed'] and g.conflicts()[0]['member_id']==old
        assert m.db.execute('SELECT vector FROM voice_profiles').fetchone()[0]==old_bytes
        state=g.resolve('新姓名','新称呼',old)
        assert state['validation']['passed'] and not state['conflicts']
        result=await g.action('save')
        assert result['member_id']==old and len(m.profiles())==1
        assert m.people.get_member(old)['name']=='新姓名'
        assert m.preferred_address(old)=='新称呼'
        assert m.people.recent_messages(old)[0]['content']=='保留的聊天'
    finally: g.cancel()


@pytest.mark.asyncio
async def test_name_conflict_can_rename_without_recollecting_and_cancel_preserves_old(tmp_path):
    from lira.personal_memory import PersonalMemory
    g,_=rig()
    m=PersonalMemory(tmp_path/'voice.db',None,'test',lambda:False)
    old=m.register_voice('同名','旧称呼',np.array([-1.,0.],np.float32))
    g.rt.personal_memory=m
    await g.begin('同名','新称呼')
    try:
        for _ in range(5): await read(g)
        assert g.validation['passed']
        with pytest.raises(ValueError,match='重复'): await g.action('save')
        g.resolve('新姓名','新称呼',None)
        result=await g.action('save')
        assert result['member_id']!=old and len(m.profiles())==2
        revision=m.voice_revision(old)
        await g.begin('同名','旧称呼',old)
        await read(g)
        g.cancel()
        assert m.voice_revision(old)==revision
    finally: g.cancel()
