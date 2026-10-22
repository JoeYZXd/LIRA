"""Read-only single-utterance test using the guided production capture lease."""
import asyncio
import time

import numpy as np

from lira.realtime_enrollment import RealtimeEnrollment
from lira.voice_enrollment import VoiceEnrollment
from lira.ui.guided_voice import GuidedVoice
from lira.speaker_candidates import top1_with_margin


class VoiceProfileTest:
    def __init__(self, handle):
        self.capture=GuidedVoice(handle)
        self.rt=handle.runtime
        self.lock=self.capture.lock
        self.member_id=None
        self.result=None

    async def begin(self, member_id):
        memory=getattr(self.rt,'personal_memory',None)
        if memory is None or type(member_id) is not int or member_id<=0:
            raise ValueError('请选择已建立声纹的成员。')
        member=memory.people.get_member(member_id)
        if member is None or not any(mid==member_id for mid,_ in memory.profiles()):
            raise ValueError('该成员尚无声纹，或档案已删除。')
        await self.capture.begin(member['name'],memory.preferred_address(member_id) or member['name'],member_id)
        self.member_id=member_id
        self.result=None
        self.capture.deadline=time.monotonic()+120
        await self.capture.action('record')
        return self.snapshot()

    def snapshot(self):
        c=self.capture
        return dict(token=c.token,recording=c.recording,
                    recording_seconds=round(time.monotonic()-c.started,1) if c.recording else 0,
                    result=self.result,message=c.message)

    def cancel(self):
        if self.capture.reserved:
            self.capture.cancel()

    async def stop(self):
        c=self.capture
        try:
            if not c.recording:raise ValueError(c.message or '录音已经结束，请重新开始测试。')
            c.recording=False
            samples=np.concatenate(c.parts) if c.parts else np.empty(0,np.float32)
            c.parts.clear()
            audio=RealtimeEnrollment._audio((samples,False),c.params)
            if audio is None or audio[1]<2:
                raise ValueError('有效说话不足 2 秒，请再测一次，建议自然说 5 到 10 秒。')
            samples,seconds=audio
            if float(np.mean(np.abs(samples)>=.99))>c.params.max_clip_ratio:
                raise ValueError('录音失真较多，请用正常音量重新测试。')
            vector=await asyncio.wait_for(asyncio.to_thread(self.rt.speaker_identity.embedding,samples[-160000:]),10)
            if self.rt.privacy.is_on or not c.token:raise ValueError('隐私模式开启或测试已结束，录音已丢弃。')
            vector=VoiceEnrollment(self.rt.personal_memory,c.params)._vector(samples,vector,references=[])
            if vector is None:raise ValueError('录音质量检查未通过，请检查音量、失真或噪声后重试。')
            identity=self.rt.speaker_identity
            details=identity.match_diagnostics(vector)
            profiles=self.rt.personal_memory.profiles()
            selected=next((v for mid,v in profiles if mid==self.member_id),None)
            if selected is None:raise ValueError('被测试档案已删除，请刷新列表。')
            selected_score=float(np.dot(vector,selected)) if selected.shape==vector.shape else None
            compatible=[(float(np.dot(vector,v)),mid) for mid,v in profiles if v.shape==vector.shape and np.isfinite(v).all()]
            winner=top1_with_margin(compatible,identity.threshold,identity.margin)[0] if details['reason']=='matched' else None
            match=self.rt.personal_memory.people.get_member(winner) if winner is not None else None
            from lira.identity_journal import IdentityJournal
            event_id=IdentityJournal(self.rt.store._conn).record(identity,vector,
                dict(details,query_speech_seconds=round(len(samples[-160000:])/16000,1)),
                source='profile_test',selected_member_id=self.member_id)
            def rounded(v):return None if v is None or not np.isfinite(v) else round(v,4)
            self.result=dict(event_id=event_id,matched=winner==self.member_id, matched_member_id=winner,
                matched_name=match['name'] if match else None, selected_score=rounded(selected_score),
                top_score=rounded(details['top_score']),threshold=details['threshold'],
                second_score=rounded(details['second_score']),score_gap=rounded(details['score_gap']),
                required_margin=details['required_margin'],reason=details['reason'],
                speech_seconds=round(seconds,1),query_seconds=round(len(samples[-160000:])/16000,1))
        finally:
            self.cancel()
        return self.snapshot()
