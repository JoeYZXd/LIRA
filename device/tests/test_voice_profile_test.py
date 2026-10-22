import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from lira.config import SpeakerConfig
from lira.personal_memory import PersonalMemory,SpeakerIdentity
from lira.ui.voice_profile_test import VoiceProfileTest


def rig(tmp_path):
    async def stop():pass
    rt=SimpleNamespace(privacy=SimpleNamespace(is_on=False),conversation_paths=SimpleNamespace(stop=stop),
        voice_enrollment=SimpleNamespace(params=SpeakerConfig()))
    rt.personal_memory=PersonalMemory(tmp_path/'test.db',None,'unused',lambda:rt.privacy.is_on)
    from lira.appliances.store import ApplianceStore
    rt.store=ApplianceStore(tmp_path/'test.db')
    rt.speaker_identity=SpeakerIdentity(rt.personal_memory,None,extractor=object())
    rt.speaker_identity.embedding=lambda samples:np.array([1.,0.],np.float32)
    handle=SimpleNamespace(runtime=rt,dev_mutex=asyncio.Lock(),recording_active=False)
    test=VoiceProfileTest(handle)
    member=rt.personal_memory.register_voice('测试成员','朋友',[1.,0.])
    return test,member


@pytest.mark.asyncio
async def test_match_uses_production_threshold_and_does_not_change_profile_or_live_result(tmp_path):
    test,member=rig(tmp_path)
    identity=test.rt.speaker_identity
    identity.last_match_details={'sentinel':True}
    before=test.rt.personal_memory.db.execute('SELECT vector FROM voice_profiles').fetchone()[0]
    await test.begin(member)
    test.capture.feed(np.full(16000*5,.1,np.float32))
    result=(await test.stop())['result']
    assert result['matched'] and result['selected_score']==1 and result['threshold']==.8
    assert identity.last_match_details=={'sentinel':True}
    assert before==test.rt.personal_memory.db.execute('SELECT vector FROM voice_profiles').fetchone()[0]
    assert test.rt.guided_voice_capture is None and not test.capture.handle.dev_mutex.locked()


@pytest.mark.asyncio
async def test_other_speaker_and_below_threshold_results(tmp_path):
    test,member=rig(tmp_path)
    other=test.rt.personal_memory.register_voice('另一人','来客',[0.,1.])
    test.rt.speaker_identity.embedding=lambda samples:np.array([0.,1.],np.float32)
    await test.begin(member);test.capture.feed(np.full(16000*3,.1,np.float32))
    result=(await test.stop())['result']
    assert not result['matched'] and result['matched_member_id']==other
    assert result['matched_name']=='另一人'
    test.rt.speaker_identity.embedding=lambda samples:np.array([.7,np.sqrt(.51)],np.float32)
    await test.begin(member);test.capture.feed(np.full(16000*3,.1,np.float32))
    result=(await test.stop())['result']
    assert not result['matched'] and result['reason']=='below_threshold'


@pytest.mark.asyncio
async def test_short_audio_privacy_and_timeout_release_capture(tmp_path):
    test,member=rig(tmp_path)
    await test.begin(member);test.capture.feed(np.full(16000,.1,np.float32))
    with pytest.raises(ValueError,match='不足'):await test.stop()
    assert not test.capture.reserved
    await test.begin(member)
    test.rt.privacy.is_on=True
    await asyncio.sleep(.3)
    assert not test.capture.token and not test.capture.reserved
    test.rt.privacy.is_on=False
    await test.begin(member);test.capture.deadline=0
    await asyncio.sleep(.3)
    assert not test.capture.token and test.rt.guided_voice_capture is None


@pytest.mark.asyncio
async def test_busy_enrollment_cannot_be_released_by_failed_test_begin(tmp_path):
    test,member=rig(tmp_path)
    marker=object();test.rt.guided_voice_capture=marker
    await test.capture.handle.dev_mutex.acquire()
    with pytest.raises(ValueError):await test.begin(member)
    test.cancel()
    assert test.rt.guided_voice_capture is marker and test.capture.handle.dev_mutex.locked()
    test.capture.handle.dev_mutex.release()
