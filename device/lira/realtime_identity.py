"""Bounded, session-local speaker discovery from ordinary cloud conversation."""
from __future__ import annotations

import asyncio
from collections import deque
import time

import numpy as np

from lira.infrared_voice import tool
from lira.realtime_enrollment import RealtimeEnrollment
from lira.voice_enrollment import VoiceEnrollment

IDENTITY_TOOLS = [tool('identify_current_speaker',
    '读取本地无感身份识别的会话结果。用户询问我是谁、认得我吗或使用登记称呼前调用。'
    '不要求用户朗读身份测试句；已识别身份可用于后续短句，换人会由本地重置。'
    '未匹配时只在用户问身份时邀请自愿录入，不自动注册，不根据自报姓名猜测。'),
    tool('get_speaker_continuity_context',
         '读取本轮本地说话人连续性状态和token。用户明确说换个人说话或需要辅助判断换人时先调用；不返回声纹或姓名。'),
    tool('report_speaker_continuity',
         '使用刚读取的token报告辅助判断。话题延续仅是弱证据，不能证明同一人；明确换人表达才用explicit_handover。本地声音证据最终决定，不认定姓名、不强制合并。',
         {'token': {'type':'string','maxLength':40},
          'assessment': {'type':'string','enum':['same_person','person_changed','uncertain']},
          'evidence': {'type':'string','enum':['conversation_flow','explicit_handover','uncertain']}}),
    tool('request_identity_confirmation','在自然停顿轻问一次低分候选身份。用户已明确回答、否认或纠正时也可调用；本地直接保存明确反馈，不会再问旧姓名。'),
    tool('submit_identity_feedback','仅依据用户本轮明确确认、否认或纠正登记姓名提交反馈。name不适用时填空字符串；不能把模型判断当用户确认。',
         {'token':{'type':'string','maxLength':40},'decision':{'type':'string','enum':['confirmed','rejected','corrected']},'name':{'type':'string','minLength':0,'maxLength':64}}),
    tool('get_recent_device_conversation','用户想接续上次聊天时读取设备最近一次真实聊天。记录归属未经当前用户确认，必须说这台设备上次聊到，不得冒充当前用户个人记忆。'),
    tool('get_personal_memory','读取当前已识别成员的个人记忆和经后台人工关联的聊天记录。未识别时不返回任何成员记忆。')]
CONTINUITY_TOOL_NAMES = {'get_speaker_continuity_context','report_speaker_continuity'}
SEARCH_SECONDS = (2, 5, 10)
TRACK_AUDIO_FRAMES = 16000 * 10  # Current, challenger and previous track: <=30s total.


class RealtimeIdentity:
    def __init__(self, runtime, *, on_change=None, on_lookup=None, on_processed=None, journal=None, session_id=lambda:None):
        self.rt, self.on_change = runtime, on_change
        self.on_lookup = on_lookup
        self.on_processed = on_processed
        self.revision = 0
        self.epoch = 0
        self._worker = None
        self.clear()
        from lira.identity_confirmation import IdentityConfirmation
        from lira.identity_journal import IdentityJournal
        self.confirmation = IdentityConfirmation(self, journal or IdentityJournal(runtime.store._conn), session_id)

    def clear_pending(self):
        self.parts = deque()
        self.frames = 0

    def _reset_search(self):
        self.identity_announced = False
        if hasattr(self, 'confirmation'):
            self.confirmation.reset()
        self.continuity_scores.clear()
        self.state = 'collecting'
        self.member_id = None
        self.anchor = None
        self.evidence = []
        self.seconds = 0.0
        self.attempts = 0
        self.pool = []
        self.pool_seconds = 0.0
        self.failures = 0
        self.history = deque()
        self.last_match = None
        self.discovery_windows = 0

    def clear(self):
        # Ownership changes independently of epoch, which fences async audio work.
        self.track_generation = getattr(self, 'track_generation', 0) + 1
        self.epoch += 1
        self.revision += 1
        self.clear_pending()
        self.utterance = None
        self.received_at = 0
        self.continuity_score = None
        self.last_continuation_floor = None
        self.processed_turns = 0
        self.input_turns = 0
        self.last_audio_seconds = 0.0
        self.last_speech_seconds = 0.0
        self.feature_extractions = 0
        self.last_result = '等待用户语音'
        self.jobs = deque(maxlen=3)
        self.challenger = deque(maxlen=3)
        self.challenger_audio = deque()
        self.previous_track = None
        self.assistance = None
        self.continuity_scores = deque(maxlen=6)
        self.last_assistance_token = None
        self._reset_search()

    async def close(self):
        self.clear()
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def feed(self, samples):
        local = getattr(self.rt, 'voice_enrollment', None)
        if self.rt.privacy.is_on or getattr(self.rt, 'speaker_identity', None) is None or local is None:
            return
        audio = np.asarray(samples, np.float32)
        if not self.parts and (not audio.size or not np.isfinite(audio).all()
                or float(np.sqrt(np.mean(audio.astype(np.float64)**2))) < local.params.min_rms):
            return
        self.parts.append(audio.copy())
        self.frames += len(audio)
        while self.frames > 16000 * 15:
            excess = self.frames - 16000 * 15
            first = self.parts.popleft()
            removed = min(excess, len(first))
            self.frames -= removed
            if removed < len(first):
                self.parts.appendleft(first[removed:])

    def note_utterance(self, text='', *, turn_number=None):
        self.confirmation.text = str(text)[:2000]
        self.revision += 1
        self.input_turns = self.input_turns+1 if turn_number is None else turn_number
        self.utterance = np.concatenate(self.parts) if self.parts else None
        self.received_at = time.monotonic()
        self.clear_pending()
        if self.rt.privacy.is_on:
            self.clear()
            return
        if len(self.jobs)==self.jobs.maxlen and self.on_processed:
            dropped=self.jobs[0]
            self.on_processed(dropped[2],{'last_result':'声纹处理队列已满，本轮跳过匹配','skipped':True},self.context())
        self.jobs.append((self.epoch, self.revision, self.input_turns, self.utterance))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
            self._worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    def snapshot(self):
        cached_frames = sum(map(len, self.history))+sum(map(len, self.challenger_audio))
        if self.previous_track:
            cached_frames += sum(map(len, self.previous_track['history']))
        return {'status': 'collecting' if self.challenger and not self.identity_announced
                and (self.state=='recognized' or self.candidate_change_pending) else self.state,
                'continuity_state': 'suspected_change' if self.challenger else 'stable',
                'suspect_probes': len(self.challenger),
                'candidate_change_pending':self.candidate_change_pending,
                'suspect_speech_seconds': round(sum(s for _, s in self.challenger), 2),
                'cached_audio_seconds': round(cached_frames / 16000, 2),
                'assistance': self._assistance(),
                'continuity_average': self.continuity_average(),
                'continuation_floor':self.last_continuation_floor,
                'continuity_windows': len(self.continuity_scores),
                'last_match': dict(self.last_match) if self.last_match else None,
                'search_attempts': self.attempts,
                'discovery_windows': self.discovery_windows,
                'speech_seconds': round(self.seconds, 2),
                'processed_turns': self.processed_turns,
                'last_audio_seconds': round(self.last_audio_seconds, 2),
                'last_speech_seconds': round(self.last_speech_seconds, 2),
                'pooled_speech_seconds': round(self.pool_seconds, 2),
                'feature_extractions': self.feature_extractions,
                'last_result': self.last_result,
                'continuity_score': (round(self.continuity_score, 4)
                                     if self.continuity_score is not None else None),
                'pending': bool(self._worker is not None and not self._worker.done())}

    def context(self):
        memory = getattr(self.rt, 'personal_memory', None)
        if self.rt.privacy.is_on:
            return {'status': 'unavailable', 'instructions': '隐私模式开启，不使用身份信息。'}
        if any(getattr(self.rt, key, None) is None for key in ('speaker_identity', 'personal_memory', 'voice_enrollment')):
            return {'status': 'unavailable', 'instructions': '本地识别未就绪，不能把故障说成未注册。'}
        if self.state == 'unavailable':
            return {'status': 'unavailable', 'instructions': '本地比对暂时失败，不能猜身份或断言未录入。'}
        if (self.challenger and not self.identity_announced
                and (self.state=='recognized' or self.candidate_change_pending)):
            return {'status': 'collecting', 'continuity_state': 'suspected_change',
                    'instructions': '正在核对声音变化，之前的累计已保留。暂不使用此前姓名，继续正常交流；可调用get_speaker_continuity_context再report_speaker_continuity提供辅助判断，不能凭话题或自报姓名认定身份。'}
        if self.state == 'recognized':
            member = memory.people.get_member(self.member_id)
            if member is None:
                self._reset_search()
            else:
                return {'status': 'recognized', 'name': member['name'],
                        'address': memory.preferred_address(self.member_id) or member['name'],
                        'identity_source': self.confirmation.source,
                        'identity_instructions':'voiceprint是声纹匹配；user_confirmed/user_corrected只是用户口头确认，不是声纹验证。',
                        'continuity_state': 'checking' if self.challenger else 'stable',
                        'continuity_average': self.continuity_average(),
                        'instructions': '这是本次对话已确认的说话者，确认来源以identity_source为准，口头确认不能声称声纹验证成功。单句波动不撤回身份。checking表示正在核对，不代表已换人。可自然使用登记称呼；使用前读取最新状态。需要记忆时调用get_personal_memory。'}
        if self.state == 'unrecognized':
            return {**self.confirmation.context(), 'status': 'unrecognized', 'instructions': '本次足量语音仍未匹配已登记声纹，已停止反复查档，按新访客正常交流。只有用户询问身份时，才说明暂未匹配到已录入声纹并邀请自愿录入；不能断言从未注册，不自动录入，不沿用此前姓名。'}
        return {**self.confirmation.context(), 'status': 'collecting', 'instructions': '正在从自然聊天累计声音，先正常回答当前问题，不主动打断，不要求朗读指定句。用户问身份时可说明还未确认，可以继续聊几句；不能猜姓名。'}

    async def identify(self):
        if self.rt.privacy.is_on:
            self.clear()
            return self.context()
        worker = self._worker
        if worker is not None and not worker.done() and worker is not asyncio.current_task():
            await asyncio.shield(worker)
        return self.context()

    async def continuity_context(self):
        await self.identify()
        detail = self.snapshot()
        return {'status':detail['status'], 'continuity_state':detail['continuity_state'],
                'continuity_average':detail['continuity_average'],
                'continuity_windows':detail['continuity_windows'],
                'token':f'{self.epoch}:{self.revision}',
                'suspect_probes':detail['suspect_probes'],
                'retained_speech_seconds':detail['speech_seconds'],
                'instructions':'仅报告当前这轮辅助判断，不询问用户朗读、不凭自报姓名认定身份。话题连贯不能证明是同一声音。'}

    async def report_continuity(self, args):
        await self.identify()
        if (self.rt.privacy.is_on or not self.processed_turns
                or args['token'] != f'{self.epoch}:{self.revision}'
                or time.monotonic()-self.received_at > 30):
            return {'status':'stale', 'instructions':'该判断已过期，不能修改当前声音累计。'}
        if args['token'] == self.last_assistance_token:
            return {'status':'duplicate', 'instructions':'本轮辅助判断已记录，不能反复修改。'}
        assessment = args['assessment']
        if assessment == 'person_changed' and args['evidence'] == 'uncertain':
            assessment = 'uncertain'
        self.assistance = {'assessment':assessment, 'time':time.monotonic(),
                           'until_turn':self.processed_turns+2}
        self.last_assistance_token = args['token']
        return {'status':'accepted', 'assessment':assessment,
                'instructions':'已记录辅助信号。没有清零或强制合并语音，仍由后续本地多段声音确认。不能宣称已认出姓名或换人已确认。'}

    async def _drain(self):
        while self.jobs:
            epoch, revision, turn_number, samples = self.jobs.popleft()
            if epoch != self.epoch:
                continue
            self.processed_turns = turn_number
            self.last_audio_seconds = len(samples) / 16000 if samples is not None else 0.0
            self.last_speech_seconds = 0.0
            before_extractions = self.feature_extractions
            try:
                await self._observe(epoch, samples)
                if (self.feature_extractions > before_extractions and self.continuity_score is not None
                        and epoch == self.epoch and not self.rt.privacy.is_on):
                    self.confirmation.journal.record(self.rt.speaker_identity,None,
                        dict(continuity_score=self.continuity_score,
                             continuity_average=self.continuity_average(),
                             continuation_floor=self.last_continuation_floor,
                             speech_seconds=self.last_speech_seconds,
                             state=self.state,identity_source=self.confirmation.source,
                             suspect_probes=len(self.challenger),
                             assessment=self._assistance()),
                        source='continuity',session_id=self.confirmation.session_id(),
                        turn=self.processed_turns,selected_member_id=self.member_id)
            except Exception:
                if epoch == self.epoch:
                    self.failures += 1
                    self.last_result = '特征提取失败或超时'
                    if self.failures >= 3:
                        self.state = 'unavailable'
                        self.member_id = None
            if revision == self.revision:
                self.utterance = None
            if epoch == self.epoch and not self.rt.privacy.is_on and self.on_processed:
                self.on_processed(turn_number,self.snapshot(),self.context())
            if epoch == self.epoch and revision == self.revision and not self.rt.privacy.is_on and self.on_change:
                try:
                    await self.on_change(self.context())
                    if self.state=='recognized' and not self.challenger:
                        self.identity_announced=True
                except Exception:
                    # Session send/close errors are owned by the realtime transport.
                    pass

    async def _observe(self, epoch, samples):
        local = getattr(self.rt, 'voice_enrollment', None)
        identity = getattr(self.rt, 'speaker_identity', None)
        memory = getattr(self.rt, 'personal_memory', None)
        if local is None or identity is None or memory is None or self.rt.privacy.is_on:
            self.state = 'unavailable'
            self.last_result = '本地声纹功能未就绪'
            return
        if self.failures >= 3:
            self.last_result = '连续失败，已暂停特征提取'
            return
        if samples is None:
            self.last_result = '本轮没有可用音频'
            return
        audio = RealtimeEnrollment._audio((samples, False), local.params)
        if audio is None:
            self.last_result = '无有效声音或音频无效'
            return
        self.last_speech_seconds = audio[1]
        if float(np.mean(np.abs(audio[0]) >= .99)) > local.params.max_clip_ratio:
            self.last_result = '削波过多，本轮未参与匹配'
            return
        self.pool.append(audio[0])
        self.pool_seconds += audio[1]
        if self.pool_seconds < max(2, local.params.min_segment_seconds):
            self.last_result = '短句累计中，尚不足提取特征'
            return
        combined = np.concatenate(self.pool)[-16000*15:]
        self.pool, self.pool_seconds = [], 0.0
        trimmed = RealtimeEnrollment._audio((combined, False), local.params)
        if trimmed is None:
            self.last_result = '累计音频无有效声音'
            return
        combined, seconds = trimmed
        vector = await self._extract(epoch, combined)
        if epoch != self.epoch or self.rt.privacy.is_on:
            return
        if vector is None:
            self.last_result = '特征或音频质量检查未通过'
            return
        self.failures = 0
        # Once locked, verify continuity against ONE anchor, not the profile bank.
        # A clearly different voice starts a fresh bounded discovery budget.
        self.continuity_score = (float(np.dot(vector, self.anchor))
                                 if self.anchor is not None else None)
        has_candidate = self.confirmation.candidate is not None
        if self.continuity_score is not None:
            self.continuity_scores.append((self.continuity_score,min(5,max(1,seconds))))
        # Continuation is distinct from first-time identification. Moderate,
        # stable scores do not revoke an identity already established at .80.
        # A candidate is still untrusted. Moderate live-voice variation should
        # not block further sampling or the human question. Use the existing
        # continuation rule; the bank's .80 identity threshold is unchanged.
        continuation_floor = .65 if self.state == 'recognized' or has_candidate else local.params.assign_threshold
        self.last_continuation_floor = continuation_floor
        average = self.continuity_average()
        low = self.continuity_score is not None and self.continuity_score < continuation_floor
        if (self.state == 'recognized' or has_candidate) and average is not None and average < continuation_floor:
            low = True
        if self.state == 'collecting' and not has_candidate:
            # There is no established person to protect yet. A short first
            # utterance is not a reliable speaker boundary: keep the bounded
            # raw window moving through the existing 2/5/10s lookup budget.
            # Candidate/recognized tracks still use the multi-probe guard below.
            await self._accept_probe(epoch, combined, vector, seconds)
            if low:
                self.last_result = '身份发现阶段单段波动，继续有界原音累计；'+self.last_result
            return
        if low:
            await self._challenge(epoch, combined, vector, seconds)
            return
        if self.challenger:
            # Revisit a borderline sample only after local audio again supports
            # the original speaker. A model opinion cannot merge distinct voices.
            compatible = (self.state == 'collecting'
                and all(float(np.dot(v, self.anchor)) >= local.params.assign_threshold-.1
                        for v, _ in self.challenger)
                and self._assistance() != 'person_changed')
            if compatible:
                replay = np.concatenate([*self.history, *self.challenger_audio, combined])[-TRACK_AUDIO_FRAMES:]
                replay_audio = RealtimeEnrollment._audio((replay, False), local.params)
                replay_vector = await self._extract(epoch, replay_audio[0]) if replay_audio else None
                if epoch != self.epoch or self.rt.privacy.is_on:
                    return
                if replay_vector is not None and float(np.dot(replay_vector, self.anchor)) >= local.params.assign_threshold:
                    recovered_windows = len(self.challenger)+1
                    recovered_seconds = sum(duration for _,duration in self.challenger)+seconds
                    self.challenger.clear()
                    self.challenger_audio.clear()
                    # The rolling raw-audio window can be shorter than the
                    # accepted history. Count each recovered segment once.
                    self.seconds += recovered_seconds
                    self.evidence = []
                    self.history.clear()
                    await self._accept_probe(epoch, replay_audio[0], replay_vector, 0,
                                             evidence_seconds=replay_audio[1], windows=recovered_windows,
                                             precombined=True)
                    self.last_result = '确认声音恢复一致，已重提取保留语音；'+self.last_result
                    return
            self.challenger.clear()
            self.challenger_audio.clear()
        await self._accept_probe(epoch, combined, vector, seconds)

    async def _extract(self, epoch, audio):
        self.feature_extractions += 1
        vector = await asyncio.wait_for(asyncio.to_thread(self.rt.speaker_identity.embedding, audio), 10)
        if epoch != self.epoch or self.rt.privacy.is_on:
            return None
        return VoiceEnrollment(self.rt.personal_memory, self.rt.voice_enrollment.params)._vector(
            audio, vector, references=[])

    @staticmethod
    def _remember(history, audio):
        # Own bounded buffers: a short numpy view must not retain a much larger
        # concatenation through its base array.
        history.append(audio[-TRACK_AUDIO_FRAMES:].copy())
        frames = sum(map(len, history))
        while frames > TRACK_AUDIO_FRAMES:
            first = history.popleft()
            remove = min(frames-TRACK_AUDIO_FRAMES, len(first))
            frames -= remove
            if remove < len(first):
                history.appendleft(first[remove:].copy())

    def _checkpoint(self):
        data = {key: getattr(self, key) for key in
                ('state','member_id','anchor','evidence','seconds','attempts','history','last_match','discovery_windows','identity_announced')}
        data['_confirmation'] = {k: getattr(self.confirmation,k) for k in
                                 ('candidate','event_id','offered','rejected','source')}
        return data

    def _assistance(self):
        hint = self.assistance
        if hint and hint['until_turn'] >= self.processed_turns and time.monotonic()-hint['time'] <= 30:
            return hint['assessment']
        return 'uncertain'

    def continuity_average(self):
        if not self.continuity_scores:
            return None
        return round(float(np.average([s for s,_ in self.continuity_scores],
                                     weights=[w for _,w in self.continuity_scores])),4)

    @property
    def candidate_change_pending(self):
        # Keep an untrusted candidate visible through one short, noisy phrase.
        # Quarantine that audio, but require sustained evidence before pausing
        # a human confirmation. Never merge the quarantined audio blindly.
        average = self.continuity_average()
        return bool(len(self.challenger) >= 2
                    and sum(s for _,s in self.challenger) >= 4
                    and average is not None and average < .65)

    async def _challenge(self, epoch, audio, vector, seconds):
        threshold = self.rt.voice_enrollment.params.assign_threshold
        if self.challenger:
            candidate = np.average(np.stack([v for v, _ in self.challenger]), axis=0,
                                   weights=[s for _, s in self.challenger])
            candidate /= np.linalg.norm(candidate)
            if float(np.dot(candidate, vector)) < threshold:
                self.challenger.clear()
                self.challenger_audio.clear()
        self.challenger.append((vector, seconds))
        self._remember(self.challenger_audio, audio)
        hint = self._assistance()
        needed_probes = 3 if hint == 'same_person' else 2
        needed_seconds = 4 if hint == 'person_changed' else 5
        locked = self.state == 'recognized'
        if locked:
            needed_probes = 2 if hint == 'person_changed' else 3
            needed_seconds = 6 if hint == 'person_changed' else 10 if hint == 'same_person' else 8
        self.last_result = '正在核对连续声音变化，保留已确认身份' if locked else '疑似换人，保留原累计并等待后续声音确认'
        if self.confirmation.candidate is not None and not locked and not self.candidate_change_pending:
            self.last_result = '单段波动或加权分数仍稳定，保留未确认候选；隔离本段声音并继续核对'
        if len(self.challenger) < needed_probes or sum(s for _, s in self.challenger) < needed_seconds:
            return
        replay = np.concatenate(self.challenger_audio)
        trimmed = RealtimeEnrollment._audio((replay, False), self.rt.voice_enrollment.params)
        confirmed = await self._extract(epoch, trimmed[0]) if trimmed else None
        if epoch != self.epoch or self.rt.privacy.is_on:
            return
        candidate = self.confirmation.candidate is not None
        change_floor = (.65 if hint == 'person_changed' else .60) if locked else .65 if candidate else threshold
        if confirmed is None or float(np.dot(confirmed, self.anchor)) >= change_floor:
            return
        if (locked or candidate) and (self.continuity_average() is None or self.continuity_average() >= change_floor):
            return
        # A second extraction must agree with the separate challenger probes.
        if any(float(np.dot(confirmed, v)) < threshold for v, _ in self.challenger):
            return
        self.track_generation += 1
        self.confirmation.pending = None
        original = self._checkpoint()
        confirmed_windows = len(self.challenger)
        previous = self.previous_track
        if previous and previous['anchor'] is not None and float(np.dot(confirmed, previous['anchor'])) >= threshold:
            for key, value in previous.items():
                if key == '_confirmation':
                    for field, saved in value.items():
                        setattr(self.confirmation,field,saved)
                else:
                    setattr(self, key, value)
        else:
            self._reset_search()
        self.previous_track = original
        self.challenger.clear()
        self.challenger_audio.clear()
        self.continuity_scores.clear()
        await self._accept_probe(epoch, trimmed[0], confirmed, trimmed[1], windows=confirmed_windows,
                                 precombined=True)
        self.last_result = '多段声音确认换人，原累计已暂存；'+self.last_result

    async def _accept_probe(self, epoch, audio, vector, seconds, *, evidence_seconds=None, windows=1,
                            precombined=False):
        self._remember(self.history, audio)
        if self.state in ('recognized', 'unrecognized'):
            self.last_result = '声音连续，沿用已确认身份' if self.state == 'recognized' else '声音连续，沿用未匹配结果，不再查档'
            return
        self.seconds += seconds
        self.discovery_windows += windows
        self.evidence.append((vector, seconds if evidence_seconds is None else evidence_seconds))
        aggregate = np.average(np.stack([v for v, _ in self.evidence]), axis=0,
                               weights=[duration for _, duration in self.evidence])
        aggregate = aggregate / np.linalg.norm(aggregate)
        self.anchor = aggregate
        if self.attempts >= len(SEARCH_SECONDS) or self.seconds < SEARCH_SECONDS[self.attempts]:
            self.last_result = '累计有效声音，等待下一查档里程碑'
            return
        # The averaged vector remains a continuity anchor, not a profile query.
        # Score exactly ONE representation: the retained original speech after
        # multiple windows, or the original extraction for a single window.
        query = vector
        method = 'concatenated_audio' if precombined else 'single_window'
        query_seconds = seconds if evidence_seconds is None else evidence_seconds
        if len(self.evidence) > 1 or len(audio) > TRACK_AUDIO_FRAMES:
            retained = RealtimeEnrollment._audio((np.concatenate(self.history),False),
                                                  self.rt.voice_enrollment.params)
            query = await self._extract(epoch,retained[0]) if retained else None
            if epoch != self.epoch or self.rt.privacy.is_on:
                return
            if query is None:
                self.last_result = '累计原音提取未通过，本次未查档'
                return
            method = 'concatenated_audio'
            query_seconds = retained[1]
        self.attempts += 1
        member_id = self.rt.speaker_identity.match(query)
        details = getattr(self.rt.speaker_identity, 'last_match_details', None)
        self.last_match = dict(details) if details else None
        query_continuity = float(np.dot(query,vector))
        # A rolling window may contain a real handover. The profile score alone
        # must not assign its older voice to the current utterance. Keep such a
        # result as an unconfirmed candidate for a human question instead.
        consistent = query_continuity >= self.rt.voice_enrollment.params.assign_threshold
        if member_id is not None and not consistent:
            member_id = None
            if self.last_match:self.last_match['reason']='inconsistent_live_voice'
        if self.last_match:
            self.last_match.update(query_method=method,query_speech_seconds=round(query_seconds,2),
                                   query_current_similarity=round(query_continuity,4),
                                   query_current_threshold=self.rt.voice_enrollment.params.assign_threshold,
                                   query_current_consistent=consistent,
                                   window_rms=float(np.sqrt(np.mean(audio.astype(np.float64)**2))),
                                   window_clip_ratio=float(np.mean(np.abs(audio)>=.99)))
        if self.last_match:
            had_candidate = self.confirmation.candidate is not None
            if not had_candidate:
                self.last_match['discovery_continuity_average']=self.continuity_average()
            self.confirmation.observe(query, self.last_match)
            self.last_match['match_event_id'] = self.confirmation.event_id
            if not had_candidate and self.confirmation.candidate is not None:
                # Candidate continuity has a new, raw-audio reference. Scores
                # against shifting discovery anchors must not veto it later.
                self.anchor=query
                self.continuity_scores.clear()
        if self.on_lookup and self.last_match and not self.rt.privacy.is_on:
            self.on_lookup(dict(self.last_match), self.processed_turns, self.attempts)
        if member_id is not None and self.rt.personal_memory.people.get_member(member_id) is not None:
            self.state, self.member_id = 'recognized', member_id
            # The profile establishes WHO; the matched live speech establishes
            # continuity. Replacing it with an older enrollment template makes
            # ordinary sentence variation look like a speaker change.
            self.anchor = query
            self.evidence = []
            self.last_result = '已匹配登记声纹'
        elif (self.seconds >= SEARCH_SECONDS[-1] and self.discovery_windows >= 2) or self.attempts >= len(SEARCH_SECONDS):
            self.state = 'unrecognized'
            self.evidence = []
            self.last_result = '足量声音仍未匹配，停止查档'
        else:
            self.last_result = '本次查档未匹配，继续累计声音'

    def accept_enrolled_member(self, member_id):
        self.clear()
        memory = getattr(self.rt, 'personal_memory', None)
        if memory is None or self.rt.privacy.is_on or memory.people.get_member(member_id) is None:
            return
        profile = next((v for mid, v in memory.profiles() if mid == member_id), None)
        if profile is not None:
            self.state, self.member_id, self.anchor = 'recognized', member_id, profile
            self.identity_announced = True
