"""Session-scoped human labels; no operation here writes a voice template."""
import re
import time
import uuid

from lira.identity_journal import CANDIDATE_THRESHOLD, IdentityJournal


class IdentityConfirmation:
    def __init__(self, speaker, journal, session_id):
        self.speaker, self.journal, self.session_id = speaker, journal, session_id
        self.reset()

    def reset(self):
        self.candidate = None
        self.event_id = None
        self.pending = None
        self.offered = False
        self.rejected = set()
        self.source = 'voiceprint'
        self.text = ''
        self.last_feedback = None
        self.unreported_feedback = None

    def observe(self, vector, details):
        s = self.speaker
        self.event_id = self.journal.record(s.rt.speaker_identity, vector, details,
            source='conversation', session_id=self.session_id(), turn=s.processed_turns)
        ranked = self.journal.ranking(s.rt.speaker_identity, vector)
        eligible = [r for r in ranked if r['score'] >= CANDIDATE_THRESHOLD]
        self.candidate = None
        if eligible and eligible[0]['member_id'] not in self.rejected:
            self.candidate = dict(eligible[0], alternatives=len(eligible), vector=vector.copy())

    def context(self):
        if not self.candidate or self.speaker.candidate_change_pending:
            return {}
        member = self.speaker.rt.personal_memory.people.get_member(self.candidate['member_id'])
        if not member:
            return {}
        return {'candidate': {'name': member['name'], 'score': round(self.candidate['score'],4),
                             'alternatives': self.candidate['alternatives'], 'confirmed': False},
                'confirmation_pending': bool(self.pending),
                'candidate_instructions': '只是未确认候选，不能称为已识别或读取其个人记忆。本地会在正常回答播完后轻问一次，请勿自行重复提问；多个候选时仍需用户确认。'}

    def _spoken_feedback(self,token,against):
        member=self.speaker.rt.personal_memory.people.get_member(against)
        first=re.split(r'[，。！？、,.!?]',self.text.strip(),maxsplit=1)[0].strip()
        negative=(first in ('不是','不是的','不是我','不对','认错了','你认错了','你认错人了','你猜错了')
                  or bool(member and ('我不是'+member['name']) in self.text))
        declared=re.search(r'(?:^|[\s，。！？、,.!?])(?:我是|我叫|叫我)([^，。！？、,.!?]{1,64})',self.text)
        if declared:
            name=re.sub(r'(?:请你?纠正|请修改)$','',declared.group(1)).strip()
            decision='confirmed' if member and member['name']==name and not negative else 'corrected'
            return dict(token=token,decision=decision,name=name)
        if negative:return dict(token=token,decision='rejected',name='')
        if re.sub(r'[\s，。！？、,.!?]','',self.text) in ('是','是的','对','对的','是我','没错','对是我','就是我','嗯','嗯对'):
            return dict(token=token,decision='confirmed',name='')
        return None

    async def request(self, *, proactive=False):
        s = self.speaker
        await s.identify()
        if s.rt.privacy.is_on or s.candidate_change_pending:
            return {'status':'unavailable'}
        if self.unreported_feedback and self.unreported_feedback[0] == s.revision:
            result=self.unreported_feedback[1]
            self.unreported_feedback=None
            return result
        if self.pending and time.monotonic() < self.pending['expires']:
            if s.revision>self.pending['revision']:
                args=self._spoken_feedback(self.pending['token'],self.pending['member_id'])
                if args:return await self.feedback(args)
            return {'status':'awaiting_feedback', 'token': self.pending['token'],
                    'instructions':'已询问过，不重复提问。等待用户明确回答。'}
        explicit_correction = (s.state == 'recognized' and any(w in self.text for w in ('不是','认错','不对','我是','我叫','叫我')))
        if (self.offered and not explicit_correction) or not self.event_id:
            return {'status':'no_candidate', 'instructions':'不重复确认身份，继续正常聊天。'}
        mid = s.member_id if s.state == 'recognized' else (self.candidate or {}).get('member_id')
        member = s.rt.personal_memory.people.get_member(mid) if mid is not None else None
        if not member:
            return {'status':'no_candidate'}
        self.pending = dict(token=uuid.uuid4().hex, expires=time.monotonic()+120,
                            revision=s.revision-1 if explicit_correction else s.revision,
                            member_id=mid, event_id=self.event_id,
                            epoch=s.epoch, delivered=not proactive, local=proactive)
        self.offered = True
        if explicit_correction:
            # A correction has already been spoken. Complete it locally instead
            # of asking the model to confirm the old name a second time.
            args=self._spoken_feedback(self.pending['token'],self.pending['member_id'])
            if args:return await self.feedback(args)
            return {'status':'feedback_requested','token':self.pending['token'],
                    'next_tool':'submit_identity_feedback',
                    'instructions':'用户正在纠正此前身份，不要再问旧姓名。根据本轮实际发言调用submit_identity_feedback。'}
        return {'status':'confirmation_requested', 'token':self.pending['token'],
                'match_event_id':self.pending['event_id'],
                'question':f'顺便确认一下，你是{member["name"]}吗？',
                'instructions':'自然问一次即可。后续用户明确回答时调用submit_identity_feedback；否认或纠正时不要争辩，不把口头确认说成声纹验证成功。'}

    async def feedback(self, args):
        s = self.speaker
        await s.identify()
        p = self.pending
        if (s.rt.privacy.is_on or s.candidate_change_pending or not p or not p.get('delivered',True) or args['token'] != p['token']
                or p['epoch'] != s.epoch or time.monotonic() >= p['expires']
                or s.revision <= p['revision']):
            return {'status':'stale', 'instructions':'没有有效的用户确认，不得猜测或更新身份。'}
        text = re.sub(r'[\s，。！？、,.!?]', '', self.text)
        decision = args['decision']
        negative = any(w in text for w in ('不是','不对','认错','猜错','弄错','否认'))
        name = args['name'].strip()
        # Only a later, explicit user transcript can label a match. Model opinion
        # and a continuity hint never constitute confirmation.
        if decision == 'confirmed':
            member=s.rt.personal_memory.people.get_member(p['member_id'])
            named_self=member and any(w+member['name'] in text for w in ('我是','我叫'))
            if negative or not (text in ('是','是的','对','对的','没错','是我','对呀','对啊','嗯','嗯是的')
                                 or text in ('对是我','就是我','当然是我','你猜对了','嗯对','对对对')
                                 or text.startswith(('是的','对的','对呀','对啊','没错','是我')) or named_self):
                return {'status':'needs_explicit_feedback'}
            mid = p['member_id']
        elif decision == 'rejected':
            if not negative:
                return {'status':'needs_explicit_feedback'}
            mid = None
        else:
            if not name or not any(w+name in text for w in ('我是','我叫','叫我')):
                return {'status':'needs_explicit_feedback'}
            matches = [m for m in s.rt.personal_memory.people.list_members()
                       if name in (m['name'], s.rt.personal_memory.preferred_address(m['id']))]
            mid = matches[0]['id'] if len(matches) == 1 else None
        self.journal.feedback(p['event_id'], decision, mid, p['member_id'], 'user_voice',
                              name if decision == 'corrected' else None)
        self.pending = None
        if decision != 'confirmed':
            self.rejected.add(p['member_id'])
        if mid is None:
            s.state, s.member_id = 'unrecognized', None
            s.identity_announced = False
            self.candidate = None
        else:
            s.state, s.member_id = 'recognized', mid
            s.identity_announced = True
            self.source = 'user_confirmed' if decision == 'confirmed' else 'user_corrected'
            if self.candidate:
                s.anchor = self.candidate['vector'].copy()
            s.evidence = []
        result = {'status':'accepted', 'decision':decision, **s.context(),
                'match_event_id':p['event_id'],
                'feedback_saved':True, 'template_updated':False}
        self.last_feedback = (s.revision,result)
        return result

    async def consume_spoken_feedback(self):
        """Only explicit later ASR text labels a match; no model guess is needed."""
        s=self.speaker
        if self.last_feedback and self.last_feedback[0]==s.revision:
            return None
        if self.pending and self.pending.get('local') and self.pending.get('delivered',True):
            if s.revision > self.pending['revision']+1:
                # A later "yes" may answer a different conversational question.
                # Never attach that to an old, unanswered identity prompt.
                self.pending=None
                return None
            args=self._spoken_feedback(self.pending['token'],self.pending['member_id'])
            if args:
                result=await self.feedback(args)
                if result.get('feedback_saved'):self.unreported_feedback=(s.revision,result)
                return result
        elif (s.state=='recognized' and any(w in self.text for w in ('不是','认错','不对'))
                and re.search(r'(?:我是|我叫|叫我)',self.text)):
            result=await self.request()
            if result.get('feedback_saved'):self.unreported_feedback=(s.revision,result)
            return result
        return None
