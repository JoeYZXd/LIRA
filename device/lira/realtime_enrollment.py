"""Cloud dialogue, local bounded voice capture; no local ASR or TTS."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

import numpy as np

from lira.infrared_voice import tool
from lira.voice_enrollment import VoiceEnrollment


DETAIL = {'type': 'string', 'minLength': 1, 'maxLength': 32}
ENROLLMENT_TOOLS = [
    tool('start_voice_enrollment', '用户明确同意录入声纹时调用，只创建本地临时会话。咨询不算同意。成功后询问姓名和希望的称呼，继续用豆包语音，不结束聊天。'),
    tool('set_voice_enrollment_details', '提取用户明确提供的姓名和希望称呼后设置资料，缺信息先询问，不推测。修改资料后必须重新念出资料并等待用户确认。', {'name': DETAIL, 'address': DETAIL}),
    tool('start_voice_sample', '准备采集下一段时调用，然后简短提示用户用正常声音说一段至少四秒的话。每次只收一段，听到用户说话后必须调用check_voice_sample检查；同一段不能重复计数。'),
    tool('check_voice_sample', '用户说完采集语料后必须调用，检查本地真实采集的音频和声纹。按返回结果继续采集或重录。ready_to_confirm时完整念出返回的姓名称呼，并让用户说我确认按这个姓名和称呼保存。'),
    tool('get_voice_enrollment_status', '查询本地声纹录入进度，不能凭对话自行判断已采集或已保存。'),
    tool('save_voice_enrollment', '仅采集达标、已念出姓名称呼、且用户随后明确确认保存时调用。本地还会核对确认语音和说话人；只有saved才报告成功，不结束豆包会话。'),
    tool('cancel_voice_enrollment', '用户说取消注册、别录了、不保存时调用，清除临时音频和向量，不删除已有声纹，继续豆包聊天。'),
    tool('reset_voice_samples', '用户明确要求重新录制本次声纹时调用。清空本次所有样本但保留姓名称呼，然后逐段重新采集；不修改已有成员声纹。'),
]
ENROLLMENT_TOOL_NAMES = {item['name'] for item in ENROLLMENT_TOOLS}
LONG_CONFIRM = '我已经核对了姓名和称呼，我确认按这个姓名和称呼保存'


def is_confirmation(text):
    clean = lambda value: re.sub(r'[\s，,。！!]', '', value)
    return clean(text) in {clean(value) for value in VoiceEnrollment.CONFIRM | {LONG_CONFIRM}}


class RealtimeEnrollment:
    def __init__(self, runtime):
        self.rt = runtime
        self.serial = 0
        self.enrollment = None
        self.last_text = ''
        self.turn = 0
        self.ready_turn = 0
        self.processing = False
        self._clear_audio()

    def _clear_audio(self):
        self.parts = []
        self.frames = 0
        self.overflow = False
        self.recording = False
        self.candidate = None

    def cancel(self):
        self.serial += 1
        if self.enrollment is not None:
            self.enrollment.cancel()
        self.enrollment = None
        self.last_text = ''
        self.processing = False
        self._clear_audio()

    def _current(self):
        enrollment = self.enrollment
        if enrollment is not None and (self.rt.privacy.is_on or time.monotonic() - enrollment.started > 240):
            self.cancel()
            return None
        return enrollment

    def snapshot(self):
        e = self._current()
        return {'status': 'idle' if e is None else e.phase,
                'segments': len(e.vectors) if e else 0,
                'speech_seconds': round(e.seconds, 2) if e else 0,
                'recording': self.recording, 'processing': self.processing}

    def feed(self, samples):
        if self._current() is None or not self.recording:
            return
        audio = np.asarray(samples, np.float32)
        if not self.parts and (not audio.size or float(np.sqrt(np.mean(audio.astype(np.float64)**2))) < self.enrollment.params.min_rms):
            return
        remaining = 16000 * 30 - self.frames
        if len(audio) > remaining:
            self.overflow = True
        if remaining > 0:
            part = audio[:remaining].copy()
            self.parts.append(part)
            self.frames += len(part)

    def note_utterance(self, text):
        self.turn += 1
        if self._current() is None:
            self.last_text = ''
            return
        self.last_text = text.strip().rstrip('。！!，, ')
        if self.last_text in VoiceEnrollment.CANCEL:
            self.cancel()
            return
        if self.recording:
            self.candidate = (np.concatenate(self.parts) if self.parts else np.empty(0, np.float32), self.overflow)
            self.parts, self.frames = [], 0
            self.overflow = False
            self.recording = self.enrollment.phase == 'confirm'

    def _arm(self):
        self._clear_audio()
        self.recording = True

    @staticmethod
    def _audio(candidate, params):
        if candidate is None or candidate[1]:
            return None
        samples = candidate[0]
        if not samples.size or not np.isfinite(samples).all():
            return None
        # Only speech-energy frames count toward duration, not prompt waiting or
        # trailing silence. Keep pauses inside the trimmed utterance for embedding.
        blocks = [samples[i:i+320] for i in range(0, len(samples), 320)]
        active = [i for i, part in enumerate(blocks)
                  if float(np.sqrt(np.mean(part.astype(np.float64)**2))) >= params.min_rms]
        if not active:
            return None
        speech_seconds = sum(len(blocks[i]) for i in active) / 16000
        return samples[active[0]*320:min((active[-1]+1)*320, len(samples))], speech_seconds

    async def call(self, name, args):
        e = self._current()
        if name == 'cancel_voice_enrollment':
            self.cancel()
            return {'status': 'cancelled', 'instructions': '本次录入已取消，未保存的音频和声纹特征已清除，可以继续聊天。'}
        if name == 'get_voice_enrollment_status':
            return self.snapshot()
        if name == 'start_voice_enrollment':
            if e is not None:
                return dict(self.snapshot(), instructions='正在录入，请继续当前步骤；不要重复开始。')
            local = getattr(self.rt, 'voice_enrollment', None)
            identity = getattr(self.rt, 'speaker_identity', None)
            memory = getattr(self.rt, 'personal_memory', None)
            if local is None or identity is None or memory is None:
                return {'status': 'unavailable', 'instructions': '本地声纹功能未配置，不能开始录入。'}
            self.cancel()
            self.enrollment = VoiceEnrollment(memory, local.params)
            self.enrollment.start('doubao')
            return {'status': 'details', 'instructions': '请告诉我你的姓名，以及希望我平时怎么称呼你。整个过程继续使用豆包语音，可随时取消。'}
        if e is None:
            return {'status': 'not_started', 'instructions': '本次录入未开始或已超时取消，请重新征得用户同意后开始。'}
        if self.processing:
            return {'status': 'busy', 'instructions': '正在检查本地声音，请稍候。'}
        if name == 'set_voice_enrollment_details':
            values = [args[key].strip() for key in ('name', 'address')]
            if any(not 1 <= len(value) <= 32 or any(ord(char) < 32 for char in value) for value in values):
                raise ValueError('Invalid details')
            if e.memory.find_member(values[0]) is not None:
                return {'status': 'name_exists', 'instructions': '此姓名已有成员，不会覆盖或关联，请到控制台核对。'}
            e.name, e.address = values
            e.phase = 'confirm' if self._enough(e) else 'samples'
            self.ready_turn = self.turn
            self._clear_audio()
            if e.phase == 'confirm':
                self._arm()
                return self._confirmation(e)
            return {'status': 'details_set', 'name': e.name, 'address': e.address,
                    'instructions': '资料已暂存。调用start_voice_sample，然后让同一个人用平时声音说至少四秒。'}
        if name == 'reset_voice_samples':
            e.vectors, e.hashes, e.seconds = [], set(), 0.0
            e.phase = 'samples' if e.name and e.address else 'details'
            self._clear_audio()
            return dict(self.snapshot(), instructions='本次样本已清空，没有修改已有声纹。重新逐段采集。')
        if name == 'start_voice_sample':
            if e.phase != 'samples':
                return {'status': 'wrong_phase', 'instructions': '请先设置姓名称呼；若已采集达标，请核对资料并确认保存。'}
            if self.recording or self.candidate is not None:
                return {'status': 'sample_pending', 'instructions': '已有采集等待完成或检查，不要重复开始，用户说完后调用check_voice_sample。'}
            self._arm()
            return dict(self.snapshot(), instructions=f'现在采集第{len(e.vectors)+1}段，请用平时的声音换一句话，至少说四秒，听完后调用check_voice_sample。')
        if name == 'check_voice_sample':
            if e.phase != 'samples':
                return self._confirmation(e) if e.phase == 'confirm' else self.snapshot()
            candidate, self.candidate = self.candidate, None
            if candidate is None:
                return {'status': 'waiting', 'instructions': '尚未收到完整采集语音，请先开始采集并等待用户说完。'}
            audio = self._audio(candidate, e.params)
            if audio is None or audio[1] < e.params.min_segment_seconds:
                return {'status': 'retry', 'instructions': '这段声音太短、太轻或超过30秒，请重新开启采集，让用户说四到十秒。'}
            samples, seconds = audio
            digest = hashlib.sha256(samples.tobytes()).digest()
            if digest in e.hashes:
                return {'status': 'retry', 'instructions': '不能重复使用同一段录音，请换一句话重新采集。'}
            vector = await self._embedding(e, samples)
            if self.enrollment is not e:
                return {'status': 'cancelled'}
            checked = e._vector(samples, vector)
            if checked is None:
                return {'status': 'retry', 'instructions': '音质不合格或与前面的声音不一致，请同一个人重新采集，避免噪声和喊叫。'}
            if len(e.vectors) >= 10:
                self.cancel()
                return {'status': 'cancelled', 'instructions': '采集次数过多仍未达标，本次未保存，请稍后重试。'}
            e.vectors.append(checked)
            e.hashes.add(digest)
            e.seconds += seconds
            if self._enough(e):
                e.phase = 'confirm'
                self.ready_turn = self.turn
                self._arm()  # Confirming speaker must match the collected voice.
                return self._confirmation(e)
            return dict(self.snapshot(), instructions='这段检查通过，但采集尚未达标。调用start_voice_sample，再请用户换话题说四到十秒。')
        if name == 'save_voice_enrollment':
            if e.phase != 'confirm' or not self._enough(e):
                return {'status': 'not_ready', 'instructions': '尚未采集达标，不能保存。'}
            if self.turn <= self.ready_turn or not is_confirmation(self.last_text):
                self._arm()
                return dict(self._confirmation(e), status='confirmation_required',
                            instructions=f'尚未收到采集完成后的明确确认。请等待本人重新完整说：{LONG_CONFIRM}。收到新的确认语音之前不要重复调用保存工具。')
            candidate, self.candidate = self.candidate, None
            audio = self._audio(candidate, e.params)
            if audio is None or audio[1] < e.params.min_segment_seconds:
                self._arm()
                return {'status': 'confirmation_required', 'instructions': f'没有收到清晰的确认语音，请本人完整说：{LONG_CONFIRM}。'}
            vector = await self._embedding(e, audio[0])
            if self.enrollment is not e:
                return {'status': 'cancelled'}
            # Verify against the same aggregate profile that will be persisted.
            # Collection still requires pairwise consistency; confirmation is
            # speaker verification against that profile, at the unchanged gate.
            profile = np.mean(e.vectors, axis=0)
            profile = profile / np.linalg.norm(profile)
            if e._vector(audio[0], vector, references=[profile]) is None:
                self._arm()
                return {'status': 'confirmation_required', 'instructions': f'确认声音太短、不清晰或与采集声音不一致，请本人用平时的声音完整说较长的确认句：{LONG_CONFIRM}。不要代替用户确认或降低检查要求。'}
            try:
                member = e.memory.register_voice(e.name, e.address, np.mean(e.vectors, axis=0), match_threshold=e.params.match_threshold)
            except Exception:
                self.cancel()
                return {'status': 'not_saved', 'instructions': '未保存：可能姓名或声音已注册，或本地存储异常。请到控制台核对，不要声称成功。'}
            self.cancel()
            return {'status': 'saved', 'member_id': member, 'instructions': '只说声纹与称呼已在本地保存，可以继续聊天；不要额外承诺已能自动认出用户。'}
        raise ValueError('Unknown enrollment tool')

    async def _embedding(self, e, samples):
        serial = self.serial
        self.processing = True
        try:
            vector = await asyncio.wait_for(asyncio.to_thread(self.rt.speaker_identity.embedding, samples), 10)
            if serial != self.serial or self._current() is not e:
                return None
            return vector
        except Exception:
            return None
        finally:
            if serial == self.serial:
                self.processing = False

    @staticmethod
    def _enough(e):
        return len(e.vectors) >= e.params.target_segments and e.seconds >= e.params.target_seconds

    @staticmethod
    def _confirmation(e):
        return {'status': 'ready_to_confirm', 'name': e.name, 'address': e.address,
                'instructions': f'采集达标，尚未保存。请完整念出：姓名是{e.name}，称呼是{e.address}。核对后请本人说：我确认按这个姓名和称呼保存。听到后调用save_voice_enrollment。'}
