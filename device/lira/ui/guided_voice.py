"""Deterministic, local enrollment using the production microphone stream."""
import asyncio
import logging
import secrets
import time

import numpy as np

from lira.realtime_enrollment import RealtimeEnrollment
from lira.voice_enrollment import VoiceEnrollment


PASSAGES = [
    '今天的天气让人觉得很舒服。我准备整理一下房间，给花浇点水，再和家人聊聊今天发生的事情。忙完以后，我想坐下来，听一首喜欢的歌。',
    '早上出门的时候，我带好了钥匙和水杯。路边有几棵绿树，小鸟正在树上唱歌。我慢慢走到路口，看看左右，再安全地穿过马路。',
    '如果周末有时间，我想和朋友一起做一顿饭。先洗青菜，再准备鸡蛋和米饭。大家一边做饭，一边聊天，最后一起收拾干净。',
    '我喜欢安静地看看书，也喜欢听有趣的故事。遇到不明白的地方，我会停下来想一想，或者请别人帮忙。每天学会一点新东西，就很开心。',
]
VALIDATION = '小鹿，现在我们来聊聊天。刚才我已经读完了几段话，这一次换一个新的话题。我想知道，你能不能通过我的声音认出我？'
logger = logging.getLogger('lira.dev')


class GuidedVoice:
    def __init__(self, handle):
        self.handle = handle
        self.rt = handle.runtime
        self.token = None
        self.lock = asyncio.Lock()
        self.parts = []
        self.clips = []
        self.vectors = []
        self.durations = []
        self.segment_checks = []
        self.recording = False
        self.deadline = 0
        self.validation = None
        self.message = ''
        self.reserved = False
        self.replace_member_id = None
        self.replace_revision = None
        self.validation_vector = None

    def snapshot(self):
        return dict(token=self.token, step=len(self.clips), total=4,
                    text=PASSAGES[len(self.clips)] if len(self.clips) < 4 else VALIDATION,
                    recording=self.recording, seconds=round(sum(self.durations), 1),
                    validation=self.validation, message=self.message,
                    segment_checks=list(self.segment_checks),
                    conflicts=self.conflicts() if len(self.vectors)==4 else [],
                    replace_member_id=self.replace_member_id,
                    name=getattr(self,'name',''), address=getattr(self,'address',''),
                    recording_seconds=round(time.monotonic()-self.started, 1) if self.recording else 0)

    async def begin(self, name, address, replace_member_id=None):
        if self.token or self.handle.dev_mutex.locked() or self.handle.recording_active:
            raise ValueError('其他录音或测试正在进行，请先结束。')
        memory = getattr(self.rt, 'personal_memory', None)
        if memory is None or getattr(self.rt, 'speaker_identity', None) is None:
            raise ValueError('设备尚未装配声纹功能。')
        for value in (name, address):
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 32 or any(ord(c)<32 for c in value):
                raise ValueError('姓名和称呼请填写 1 到 32 个字。')
        self.select_replacement(replace_member_id)
        await self.handle.dev_mutex.acquire()
        self.reserved = True
        try:
            await self.rt.conversation_paths.stop()
            self.name, self.address = name.strip(), address.strip()
            self.params = self.rt.voice_enrollment.params
            self.token = secrets.token_urlsafe(24)
            self.clips, self.vectors, self.durations = [], [], []
            self.segment_checks = []
            self.validation, self.message = None, ''
            self.validation_vector = None
            self.deadline = time.monotonic()+600
            self.rt.guided_voice_capture = self
            self.watchdog = asyncio.create_task(self._watch())
        except BaseException:
            self.handle.dev_mutex.release()
            self.reserved = False
            raise

    def feed(self, samples):
        # Called by the same 16 kHz capture path used during real conversations.
        if self.rt.privacy.is_on:
            self.parts.clear()
            self.recording = False
            return
        if self.recording and time.monotonic()-self.started < 40:
            self.parts.append(np.asarray(samples, np.float32).copy())

    def interrupt(self):
        self.parts.clear()
        self.recording = False
        self.message = '麦克风输入中断，本段已丢弃。请等待设备恢复后重读。'

    async def _watch(self):
        while self.token:
            await asyncio.sleep(.2)
            if self.rt.privacy.is_on or time.monotonic() > self.deadline:
                async with self.lock:
                    self.cancel()
                return
            if self.recording and time.monotonic()-self.started >= 40:
                self.recording = False
                self.parts.clear()
                self.message = '本段超过 40 秒，已停止并丢弃。请准备好后重新录制。'

    def cancel(self):
        self.recording = False
        self.parts, self.clips, self.vectors, self.durations = [], [], [], []
        self.segment_checks = []
        self.validation = None
        self.validation_vector = None
        self.replace_member_id = self.replace_revision = None
        self.token = None
        self.rt.guided_voice_capture = None
        if self.reserved:
            self.handle.dev_mutex.release()
            self.reserved = False
        task = getattr(self, 'watchdog', None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def action(self, action):
        if action == 'cancel':
            self.cancel()
            return {'cancelled': True}
        if action == 'record':
            if self.recording or self.validation and self.validation['passed']:
                raise ValueError('请先完成当前步骤。')
            self.parts = []
            if len(self.clips)==4:
                self.validation = None
                self.validation_vector = None
            self.started = time.monotonic()
            self.recording = True
            self.message = '正在使用 LIRA 麦克风录音。读完后点击“读完了”。'
        elif action == 'stop':
            if not self.recording:
                raise ValueError(self.message or '当前没有录音，请重新开始。')
            self.recording = False
            samples = np.concatenate(self.parts) if self.parts else np.empty(0, np.float32)
            self.parts = []
            audio = RealtimeEnrollment._audio((samples, False), self.params)
            if audio is None or audio[1] < 8:
                seconds = 0 if audio is None else audio[1]
                raise ValueError(f'本段未通过：有效说话不足，仅 {seconds:.1f} 秒，需要至少 8 秒。前面已通过的段落保留；点击“重新录制本段”后自然读完整段，可再补充说说今天的事情。')
            samples, seconds = audio
            if np.mean(np.abs(samples) >= .99) > self.params.max_clip_ratio:
                raise ValueError('声音出现失真，请稍微离远一点，用正常音量重读本段。')
            vector = await asyncio.to_thread(self.rt.speaker_identity.embedding, samples[:160000])
            if self.rt.privacy.is_on or not self.token:
                self.cancel()
                raise ValueError('隐私模式已开启，本次录音已丢弃。')
            checker = VoiceEnrollment(self.rt.personal_memory, self.params)
            checked = checker._vector(samples, vector, references=[])
            if checked is None:
                raise ValueError('本段声音质量检查未通过，前面通过的段落已保留。请检查音量和环境噪声，再重新录制本段。')
            vector = checked
            check = {'segment': len(self.clips)+1, 'status': 'reference', 'score': None}
            if len(self.clips)<4 and self.vectors:
                reference = self.template()
                score = float(np.dot(vector, reference)) if vector.shape == reference.shape else -1
                # Reuse the configured weak-association floor only to reject
                # obvious capture anomalies. It never authorizes identity/save.
                floor = self.params.assign_threshold
                if score < floor:
                    raise ValueError(f'本段声音一致性未通过：与综合模板分数 {score:.4f}，低于异常检查线 {floor:.4f}。此前采集段落保留；请确认同一人、正常距离和音量，重新录制本段。')
                check.update(score=round(score,4), status='borderline' if score < self.params.consistency_threshold else 'consistent',
                             warning_threshold=self.params.consistency_threshold, anomaly_threshold=floor)
            if len(self.clips) < 4:
                self.clips.append(samples.copy())
                self.vectors.append(vector)
                self.durations.append(seconds)
                self.segment_checks.append(check)
                logger.info('dev event=voice_guided_segment step=%d status=%s centroid_score=%s',
                            len(self.clips), check['status'], check['score'])
                self.message = '本段已采集。休息一下，再读下一段。' if len(self.clips)<4 else '四段采集完成。现在用一段新文案验证，验证录音不会用于建档。'
                if check['status'] == 'borderline':
                    self.message = f'本段与综合模板的一致性处于临界区（{score:.4f}），已保留，无需反复重读。' + self.message + '最终是否可保存由独立验证决定。'
            else:
                self.validation_vector = vector.copy()
                self.evaluate_validation()
        elif action == 'save':
            if not self.validation or not self.validation['passed']:
                raise ValueError('独立验证通过后才能保存。')
            self.evaluate_validation()
            if not self.validation['passed']:
                raise ValueError('档案发生变化，当前独立验证未通过，不能保存。')
            if self.conflicts():
                raise ValueError('存在姓名或声音重复，请选择替换已有档案，或修改姓名。录音和验证结果已保留。')
            member = self.rt.personal_memory.save_guided_voice(self.name, self.address, self.template(),
                replace_member_id=self.replace_member_id, expected_revision=self.replace_revision,
                match_threshold=self.params.match_threshold)
            self.cancel()
            return {'saved': True, 'member_id': member}
        else:
            raise ValueError('未知操作。')
        return self.snapshot()

    def select_replacement(self, member_id):
        if member_id is not None and (type(member_id) is not int or member_id<=0):
            raise ValueError('替换目标无效。')
        revision = self.rt.personal_memory.voice_revision(member_id) if member_id is not None else None
        self.replace_member_id, self.replace_revision = member_id, revision

    def conflicts(self):
        memory=self.rt.personal_memory
        template=self.template()
        profiles=dict(memory.profiles())
        conflicts=[]
        for member in memory.people.list_members():
            mid=member['id']
            if mid==self.replace_member_id:
                continue
            reasons=[]
            if member['name']==self.name: reasons.append('姓名重复')
            profile=profiles.get(mid)
            if profile is not None and profile.shape==template.shape and float(np.dot(template,profile))>=self.params.match_threshold:
                reasons.append('声音相似')
            if reasons:
                conflicts.append(dict(member_id=mid,name=member['name'],reason='、'.join(reasons)))
        return conflicts

    def resolve(self, name, address, member_id):
        if len(self.vectors)!=4:
            raise ValueError('采集完成后才能处理保存方式。')
        for value in (name,address):
            if not isinstance(value,str) or not 1<=len(value.strip())<=32 or any(ord(c)<32 for c in value):
                raise ValueError('姓名和称呼请填写 1 到 32 个字。')
        self.select_replacement(member_id)
        self.name,self.address=name.strip(),address.strip()
        if self.validation_vector is not None:
            self.evaluate_validation()
        else:
            self.message='保存方式已更新，请继续独立验证。'
        return self.snapshot()

    def evaluate_validation(self):
        vector=self.validation_vector
        score=float(np.dot(self.template(),vector))
        other=[float(np.dot(v,vector)) for mid,v in self.rt.personal_memory.profiles()
               if mid!=self.replace_member_id and v.shape==vector.shape]
        gap=score-max(other) if other else None
        passed=score>=self.params.match_threshold and (gap is None or gap>=self.params.match_margin)
        self.validation=dict(passed=passed,score=round(score,4),threshold=self.params.match_threshold,
            margin=None if gap is None else round(gap,4),required_margin=self.params.match_margin)
        self.message='独立验证通过，可以保存档案。' if passed else '独立验证未通过，尚未保存。请检查重复档案提示，或保持日常说话方式重试验证。'

    def template(self):
        # Fixed recipe, never select the most favorable score or use held-out audio.
        vector = np.mean(self.vectors, axis=0)
        return vector/np.linalg.norm(vector)
