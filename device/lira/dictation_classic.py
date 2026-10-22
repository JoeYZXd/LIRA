"""Classic local ASR / configured text model / local TTS teaching adapter.

Business decisions and TV controls use the same agents and durable tools as
the parent console and H5 regression. This adapter only owns voice lifecycle.
"""
import asyncio
import logging
import uuid

from .dictation_agent import DictationAgent
from .dictation_tools import DictationDialogueAgent, DictationTools

LOG = logging.getLogger(__name__)


class ClassicDictation:
    def __init__(self, runtime):
        self.rt = runtime
        paths = runtime.conversation_paths
        source = paths.dictation
        self.service = paths.dictation_service
        self._session = 'classic-' + uuid.uuid4().hex
        self.tools = DictationTools(
            self.service, lambda: self.rt.voice_member_id,
            lambda: self._session, device_id=source.device_id,
            callback_id=source.callback_id,
            identity_stamp=lambda: (self.rt.voice_member_id,
                                   self.service.owner_epoch(self.rt.voice_member_id)))
        self.preparation = None
        self.dialogue = None
        self._member = None
        self._member_epoch = None
        self._operation = None
        self._completions = []
        self._active = None

    def accepts(self, text):
        from .dialog.intents import is_cancel, is_shutdown_phrase, is_status_query, match_dnd_phrase, match_volume_word
        if (self.rt.privacy.is_on or self.rt.voice_member_id is None
                or self.rt.conversation_paths.mode != 'classic'):
            return False
        if (text.strip() in ('结束聊天', '退出') or is_cancel(text)
                or is_shutdown_phrase(text) or is_status_query(text)
                or match_dnd_phrase(text) is not None or match_volume_word(text) is not None):
            return False
        state = self.tools.coordinator.status()
        return bool('默写' in text or self.preparation is not None
                    or state and state['state'] not in ('completed', 'cancelled'))

    def current(self):
        return (not self.rt.privacy.is_on and self.rt.voice_member_id == self._member
                and self.rt.engine.is_current(self._operation))

    async def speak(self, text):
        from .dialog.state_machine import State
        if not self.current():
            return False
        self.rt.engine._disarm()
        self.rt.engine._transition(State.SPEAKING)
        completion = self.rt.callbacks.speak(text, owner=self._operation)
        if completion is None:
            return False
        self._completions.append(completion)
        await completion.wait()
        return self.current() and getattr(completion, 'status', None) == 'success'

    async def handle(self, text, *, utterance_id=None):
        # Adoption is performed only after the caller's actual voiceprint match.
        if self.rt.voice_member_id is not None and not self.rt.privacy.is_on:
            epoch = self.service.owner_epoch(self.rt.voice_member_id)
            if self._member != self.rt.voice_member_id or self._member_epoch != epoch:
                await self.tools.close()
                # A verified fresh input owns a fresh coordinator, including
                # when there is no delivered task to adopt yet.
                self.tools.coordinator.task_id = None
                self.tools.coordinator.closed = False
                self.preparation = self.dialogue = None
                self._session = 'classic-' + uuid.uuid4().hex
                self._member = self.rt.voice_member_id
                self._member_epoch = epoch
            previous_task = self.tools.coordinator.task_id
            self.tools.adopt_active()
            if self.tools.coordinator.task_id != previous_task:
                self.preparation = self.dialogue = None
        if not self.accepts(text):
            return False
        from .dialog.state_machine import State
        engine = self.rt.engine
        engine.note_utterance(utterance_id)
        engine._accept_turn(text)
        self._operation = engine._new_operation()
        engine._disarm()
        engine._transition(State.PROCESSING)
        self._completions = []
        self._active = asyncio.current_task()
        llm = self.rt._llm
        options = llm._voice_options(True) if hasattr(llm, '_voice_options') else {}
        try:
            previous = self.tools.coordinator.status()
            if previous and previous['state'] in ('completed', 'cancelled'):
                self._session = 'classic-' + uuid.uuid4().hex
                self.preparation = self.dialogue = None
            self.tools.event('user_text', text)
            state = self.tools.coordinator.status()
            if state and state['state'] not in ('completed', 'cancelled'):
                if self.dialogue is None:
                    self.dialogue = DictationDialogueAgent(
                        self.tools, llm._client, llm._model,
                        speak=self.speak, request_options=options)
                result = await self.dialogue.turn(text, turn=self.tools.coordinator.turn)
            else:
                if self.preparation is None:
                    self.preparation = DictationAgent(
                        llm._client, llm._model, self.service,
                        owner_id=self._member, session_id=self.tools.session_id(),
                        paired_device_id=self.tools.device_id,
                        callback_registration_id=self.tools.callback_id,
                        request_options=options)
                result = await self.preparation.respond(text)
                if not self.current():
                    return True
                if result['status'] == 'ready':
                    task = result['task']
                    delivery = await self.tools.call('deliver_dictation_task', {'task_id': task['task_id']})
                    self.preparation = None
                    self.dialogue = None
                    await self.speak('默写任务已准备好，请在电视上确认开始。' if delivery.get('status') != 'error'
                                     else '任务已准备好，电视暂时未连接，请稍后确认。')
                elif result.get('text'):
                    await self.speak(result['text'])
            for entry in result.get('trace', []):
                LOG.info('dictation classic event=tool_result tool=%s status=%s',
                         entry.get('tool'), entry.get('result', {}).get('status'))
            LOG.info('dictation classic event=turn_complete status=%s model=%s task_id=%s',
                     result['status'], llm._model, self.tools.coordinator.task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception('dictation classic event=turn_failed')
            if self.current() and self.tools.coordinator.allow_output():
                await self.speak('这次默写交互暂时失败，请查看后台调试日志。')
        finally:
            self._active = None
            if self.current():
                # Replay the already drained completion into the existing engine
                # lifecycle; never synthesize or enqueue the same answer twice.
                if self._completions:
                    engine._speak_answer('', owner=self._operation, user_text=text,
                                         record=False, streamed=self._completions)
                else:
                    engine._begin_listening(replay=False)
                    engine._transition(State.FOLLOWUP)
                    engine._arm(engine.FOLLOWUP_WINDOW_SECONDS)
        return True

    async def apply_privacy(self, enabled):
        if enabled:
            await self.close()

    async def close(self):
        if self._active and self._active is not asyncio.current_task():
            self._active.cancel()
        self.preparation = self.dialogue = None
        self._member_epoch = None
        await self.tools.close()
