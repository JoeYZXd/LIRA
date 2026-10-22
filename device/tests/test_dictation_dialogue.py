import pytest
from lira.dictation_tools import DictationCoordinator

class Repo:
 def __init__(self): self.value=dict(task_id='t',state='playing',state_revision=3,run_epoch=1,actual=dict(phase_id='dictation',item_id='i',boot_id='b',session_id='s'))
 def status(self,t): return dict(self.value)
 def snapshot(self,t): return dict(content_hash='h')
class Results:
 def __init__(self):self.rows=[]
 def record_assistance(self,*args):self.rows.append(args)
class Service:
 def __init__(self):self.repository=Repo();self.results=Results();self.privacy=lambda:False
 async def control_task(self,c,owner_id):
  self.command=c
  return dict(status='pending')

@pytest.mark.asyncio
async def test_pause_requires_actual_receipt_and_drain():
 s=Service();c=DictationCoordinator(s,lambda:1);c.bind('t')
 c.observe_user('这个词是什么意思',turn=1)
 assert not c.allow_output()
 assert (await c.control('pause',timeout=.02))['status']=='pending'
 assert not c.allow_output()
 s.repository.value.update(state='paused',state_revision=4)
 s.repository.value['actual'].update(action='paused',command_id=s.command['command_id'])
 assert c.allow_output()
 c.answer_started();c.observe_user('继续',turn=2)
 assert (await c.control('resume',timeout=.01))['status']=='answer_not_drained'
 c.answer_drained('解释',turn=1)
 assert len(s.results.rows)==1
 assert (await c.control('resume',timeout=.01))['status']=='pending'
 assert s.command['action']=='resume'
 assert (await c.control('resume',timeout=.01))['status']=='confirmation_required'

@pytest.mark.asyncio
async def test_identity_revision_and_model_confirmation_cannot_resume():
 s=Service();c=DictationCoordinator(s,lambda:1);c.bind('t')
 s.repository.value.update(state='paused',state_revision=4)
 c.observe_user('继续',turn=2)
 s.repository.value['state_revision']=5
 assert (await c.control('resume'))['status']=='confirmation_required'
 c.close();assert not c.allow_output()

from test_dictation_service import service

@pytest.mark.asyncio
async def test_formal_service_transport_pause_then_actual_output_drain(service):
 import json, asyncio, httpx
 from lira.dictation_tools import DictationTools
 from lira.audio.doubao_realtime import RealtimeSession, RealtimeConfig
 from test_dictation_transport import event
 tools=DictationTools(service,lambda:1,lambda:'s',device_id='tv')
 await tools.call('get_dictation_context',dict(scope_type='lesson',start=2))
 result=await tools.call('prepare_dictation_task',dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}}))
 t=result['task'];p=t['phases'][1];iid=p['items'][0]['item_id'];outbox=[event(t,1,'playback_started','playing',phase_id=p['phase_id'],item_id=iid)];commands=[]
 def handle(req):
  body=json.loads(req.content)
  if req.url.path.endswith('/events'):return httpx.Response(200,json={'events':[e for e in outbox if e['sequence']>body['after']]})
  if req.url.path.endswith('/control'):
   commands.append(body)
   if body['action']=='pause':outbox.append(event(t,2,'paused','paused',command_id=body['command_id'],phase_id=p['phase_id'],item_id=iid))
  return httpx.Response(200,json={'status':'ok'})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
  tr=service.attach_transport(client,base='http://tv',token='secret',device_id='tv');await tr.poll(t['task_id'])
  tools.coordinator.observe_user('这个词怎么写',turn=1)
  played=[]
  class Player:
   async def play_stream(self,chunks):
    async for chunk in chunks:played.append(chunk)
   async def stop(self):pass
  session=RealtimeSession(config=RealtimeConfig(),player=Player(),on_event=lambda *a:None,on_tool=None)
  session.dictation_coordinator=tools.coordinator;session.capabilities=True
  await session._start_output();assert not played and session.output is None
  assert not await session.speak_text('不能提前解释')
  assert (await tools.call('control_dictation_task',{'action':'pause'}))['status']=='applied'
  assert tools.coordinator.allow_output()
  tools.coordinator.observe_user('继续',turn=2)
  assert (await tools.call('control_dictation_task',{'action':'resume'}))['status']=='answer_not_drained'
  await session._start_output();await session._enqueue_output(session.output,b'\x01\x00');await session._enqueue_output(session.output,None)
  outbox.append(event(t,3,'resume_requested','paused',phase_id=p['phase_id'],item_id=iid));await tr.poll(t['task_id'])
  assert tools.coordinator.allow_output()
  session._agreement_delivery['text']='尽头是末尾的意思'
  await session.playback;await asyncio.sleep(0)
  session._publish_agreement_delivery()
  assert played and not tools.coordinator.answer_pending
  assert len(service.results.facts(1,'b',1,2)['assistance'])==1
  assert (await tools.call('control_dictation_task',{'action':'resume'}))['status']=='pending'
  assert [c['action'] for c in commands]==['pause','resume']
  tools.coordinator.close()

@pytest.mark.asyncio
async def test_forget_removes_context_pcm_and_blocks_late_preparation(service):
 args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}})
 prepared=await service.prepare_task(args,owner_id=1,session_id='s',paired_device_id='tv',callback_registration_id='cb')
 from pathlib import Path
 folder=Path(prepared['folder']);assert folder.exists()
 service.forget(1)
 assert not folder.exists() and not service.sessions
 assert service.db.execute('SELECT count(*) FROM dictation_preparation_context').fetchone()[0]==0
 with pytest.raises(ValueError,match='forgotten'):await service.prepare_task(args,owner_id=1,session_id='late',paired_device_id='tv',callback_registration_id='cb')

@pytest.mark.asyncio
async def test_real_input_one_shot_confirm_and_old_answer_ticket():
 s=Service();c=DictationCoordinator(s,lambda:1);c.bind('t')
 s.repository.value.update(state='waiting_child')
 assert (await c.control('confirm'))['status']=='child_confirmation_required'
 c.observe_user('写好了',turn=1)
 assert (await c.control('confirm',timeout=.01))['status']=='pending'
 assert (await c.control('confirm'))['status']=='child_confirmation_required'
 s.repository.value.update(state='paused',state_revision=4)
 c.observe_user('怎么写',turn=2);ticket=c.answer_started()
 c.observe_user('是什么意思',turn=3)
 c.answer_drained('过时回复',turn=2,ticket=ticket)
 assert not s.results.rows and c.answer_pending
 c.input_started();assert not c.allow_output()


@pytest.mark.asyncio
async def test_runtime_exposes_v2_without_enabling_other_experiments():
 from test_conversation_paths import runtime
 from lira.audio.doubao_realtime import TOOLS
 from lira.dictation import DICTATION_TOOL_NAMES
 rt,paths=runtime()
 assert not paths.capabilities_enabled
 assert not {s['name'] for s in TOOLS}&DICTATION_TOOL_NAMES
 await paths.switch('doubao_realtime');await paths.start()
 offered=set(paths.session.kwargs['capabilities'].entries)
 assert offered=={'get_dictation_context','prepare_dictation_task','deliver_dictation_task','get_dictation_task_status','control_dictation_task'}
 assert (await paths._tool_impl('get_dictation_material',{}))['reason']=='legacy_dictation_disabled'
 await paths.close()

@pytest.mark.asyncio
async def test_wrong_pause_ack_identity_change_and_late_drain_do_not_unlock():
 s=Service();identity=[1];c=DictationCoordinator(s,lambda:1,lambda:identity[0]);c.bind('t')
 c.observe_user('怎么写',turn=1)
 await c.control('pause',timeout=.01)
 s.repository.value.update(state='paused',state_revision=4)
 s.repository.value['actual'].update(action='paused',command_id='old-pause')
 assert not c.allow_output()
 s.repository.value['actual']['command_id']=s.command['command_id']
 assert c.allow_output();ticket=c.answer_started()
 identity[0]=2
 assert not c.allow_output()
 c.answer_drained('旧回答',turn=1,ticket=ticket)
 assert not s.results.rows
 assert (await c.control('resume'))['status']=='unavailable'
 c.close();c.answer_drained('迟到回调',turn=1)
 assert not s.results.rows

@pytest.mark.asyncio
async def test_adopts_only_unambiguous_delivered_same_child_task(service):
 from lira.dictation_tools import DictationTools
 from test_dictation_transport import event
 args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}})
 result=await service.prepare_task(args,owner_id=1,session_id='ui',paired_device_id='tv',callback_registration_id='cb')
 tools=DictationTools(service,lambda:1,lambda:'voice',device_id='tv')
 assert tools.adopt_active()['status']=='no_active_task'
 service.repository.receive(event(result['task'],1),paired_device_id='tv')
 assert tools.adopt_active()['task_id']==result['task']['task_id']
 await tools.close()
 other=await service.prepare_task(args,owner_id=1,session_id='ui2',paired_device_id='tv',callback_registration_id='cb')
 service.repository.receive(event(other['task'],1),paired_device_id='tv')
 assert tools.adopt_active()['status']=='ambiguous_tasks'
 # A later UI task is adopted on real child input once the old task is terminal.
 service.db.execute("UPDATE dictation_tasks SET state='cancelled' WHERE task_id=?",(result['task']['task_id'],));service.db.commit()
 tools.event('user_text','开始吧')
 assert tools.coordinator.task_id==other['task']['task_id']
 await tools.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['cancelled','completed'])
@pytest.mark.parametrize('first_observer', ['output','drain','user'])
async def test_terminal_task_releases_dialogue_without_late_assistance(terminal,first_observer):
 s=Service();c=DictationCoordinator(s,lambda:1);c.bind('t')
 c.observe_user('怎么写',turn=1)
 await c.control('pause',timeout=.01)
 s.repository.value.update(state='paused',state_revision=4)
 s.repository.value['actual']['command_id']=s.command['command_id']
 ticket=c.answer_started()
 s.repository.value.update(state=terminal,state_revision=5)
 if first_observer=='drain':c.answer_drained('late',turn=1)
 if first_observer=='user':c.observe_user('hello',turn=2)
 assert c.allow_output()
 assert not c.question and not c.answer_pending and not c.speaking
 c.answer_drained('迟到辅导',turn=1,ticket=ticket)
 assert not s.results.rows
 c.observe_user('今天天气怎么样',turn=2)
 assert c.allow_output()
 assert (await c.control('resume'))['status']=='confirmation_required'
 c.input_started();assert not c.allow_output()
 c.observe_user('你好',turn=3);s.privacy=lambda:True
 assert not c.allow_output()

@pytest.mark.asyncio
@pytest.mark.parametrize('voice_path',['realtime','classic'])
async def test_adopted_task_pinned_skill_reaches_both_voice_model_contexts(service,voice_path):
 from lira.dictation_tools import DictationTools
 from lira.audio.doubao_realtime import RealtimeSession, RealtimeConfig
 from lira.llm import LlmClient
 from lira.config import LlmConfig
 from types import SimpleNamespace
 old=service.skills.get();args=dict(scope_type='lesson',start=2,proposals={'2':{'words':['\u5c3d\u5934','\u4f9d\u9760']}})
 task=(await service.prepare_task(args,owner_id=1,session_id='ui',paired_device_id='tv',callback_registration_id='cb'))['task']
 service.skills.save('NEW SKILL MUST NOT REPLACE PINNED CONTENT',expected_revision=old['revision'])
 tools=DictationTools(service,lambda:1,lambda:'voice')
 assert tools.adopt(task['task_id'])['status']=='adopted'
 session=RealtimeSession(config=RealtimeConfig(),player=None,on_event=lambda *a:None,on_tool=None)
 session.dictation_coordinator=tools.coordinator
 sent=[]
 async def send(kind,**data):sent.append((kind,data))
 session.send=send;session.ws=True
 if voice_path=='realtime':
  await session._capability_context_changed()
  instructions=sent[-1][1]['session']['instructions']
  assert old['content'] in instructions and 'NEW SKILL MUST' not in instructions
  # Real transcript handling pushes a changed binding before processing output.
  import json
  class Socket:
   calls=0
   async def recv(self):
    self.calls+=1
    if self.calls>1:session.closed=True
    return json.dumps({'type':'conversation.item.input_audio_transcription.completed','text':'question'})
  session.ws=Socket();sent.clear()
  await session._receive()
  assert sent[0][0]=='session.update'
  assert old['content'] in sent[0][1]['session']['instructions']
  await tools.close()
  return
 captured=[]
 class Stream:
  def __aiter__(self):return self
  async def __anext__(self):raise StopAsyncIteration
  async def close(self):pass
 async def create(**kwargs):captured.append(kwargs);return Stream()
 client=LlmClient(LlmConfig('http://unit.test','key','unit',10,3,60,2,150),openai_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
 client.dictation_context_provider=getattr(tools.coordinator,'model_instructions',lambda:old['content'])
 await client.chat('这个词是什么意思')
 messages=captured[-1]['messages']
 assert any(old['content'] in m['content'] for m in messages)
 assert not any('NEW SKILL MUST' in m['content'] for m in messages)
 from lira.llm.prompts import PLAIN_SPEAK_SYSTEM_PROMPT
 await client.chat('reading text',system=PLAIN_SPEAK_SYSTEM_PROMPT)
 assert not any(old['content'] in m['content'] for m in captured[-1]['messages'])
 await client.chat('text chat',voice_log=False)
 assert not any(old['content'] in m['content'] for m in captured[-1]['messages'])
 await tools.close()

@pytest.mark.parametrize('phrase',['上一词','上一个','上一个词','再读一遍'])
def test_manual_navigation_intent_does_not_create_question(phrase):
 s=Service();c=DictationCoordinator(s,lambda:1);c.bind('t')
 s.repository.value.update(state='waiting_child')
 c.observe_user(phrase,turn=1)
 assert c.question is None and not c.answer_pending
 s.repository.value['actual']['item_id']='previous-item'
 c.observe_user('这个词是什么意思',turn=2)
 assert c.question[0][5]=='previous-item'

@pytest.mark.parametrize('phrase',['下一词','下一个词'])
@pytest.mark.asyncio
async def test_manual_next_word_requires_real_child_ack(phrase):
 s=Service();c=DictationCoordinator(s,lambda:1);c.bind('t')
 s.repository.value.update(state='waiting_child')
 c.observe_user(phrase,turn=1)
 assert c.question is None and c.child_ack==(c.stamp(),1)
 assert (await c.control('confirm',timeout=.01))['status']=='pending'
 assert s.command['action']=='confirm'

@pytest.mark.parametrize('action',['previous','replay','confirm'])
@pytest.mark.asyncio
async def test_manual_navigation_cannot_bypass_pending_explanation(action):
 s=Service();c=DictationCoordinator(s,lambda:1);c.bind('t')
 s.repository.value.update(state='waiting_child')
 c.observe_user('山顶是什么意思',turn=1)
 c.observe_user('下一词',turn=2)
 assert (await c.control(action,timeout=.01))['status']=='answer_not_drained'
 assert not hasattr(s,'command')

@pytest.mark.asyncio
async def test_model_status_uses_bound_playback_not_latest_settings(service):
 from lira.dictation_tools import DictationTools,DictationDialogueAgent
 from copy import deepcopy
 config=deepcopy(service.settings.get_v2()['parameters']);config['playback'].update(manual_navigation=True,repeat_count=1)
 service.settings.save_v2(config,expected_revision=1)
 tools=DictationTools(service,lambda:1,lambda:'frozen',device_id='tv')
 result=await service.prepare_task(dict(scope_type='lesson',start=2,proposals={'2':{'words':['尽头','依靠']}}),owner_id=1,session_id='frozen',paired_device_id='tv',callback_registration_id='cb')
 tools.coordinator.bind(result['task']['task_id'])
 latest=deepcopy(config);latest['playback']['manual_navigation']=False
 service.settings.save_v2(latest,expected_revision=2)
 status=await tools.call('get_dictation_task_status',{})
 assert status['playback']['manual_navigation'] is True
 assert '"manual_navigation": true' in tools.coordinator.model_instructions()
 agent=DictationDialogueAgent(tools,None,'kimi')
 assert '"manual_navigation": true' in agent.messages[0]['content']
