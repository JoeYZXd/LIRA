"""Trusted dialogue ordering around durable TV events; no pedagogical decisions."""
import asyncio
import uuid
import re
import hashlib
import json
import httpx

INSTRUCTIONS = ('语文默写使用get_dictation_context和prepare_dictation_task，读取业务skill并由模型决定词序。'
 '准备后deliver_dictation_task，start需孩子确认。执行期间孩子提问必须先control_dictation_task(action=pause)，'
 '只有实际暂停回执后才能解释。回答后询问是否继续，真实孩子确认且回答音频排空后才能resume；'
 'pending不是成功，不可用pause_agent_task代替电视暂停。写好了或下一词用confirm，重播用replay，上一词用previous，结束用cancel。手动模式每次只播报一遍，等待用户控制，不自行重播或推进。')

class DictationCoordinator:
 def __init__(self,service,owner,identity_stamp=None):
  self.service,self.owner=service,owner;self.identity_stamp=identity_stamp or owner;self.bound_identity=None;self.task_id=None;self.closed=False
  self.bound_owner=None;self.question=None;self.pause_command=None;self.confirmation=None;self.speaking=False;self.turn=0;self.input_pending=False;self.child_ack=None;self.answer_pending=False;self.last_remote_sequence=0;self.remote_requested=False
 def bind(self,task_id):
  if self.task_id==task_id:return
  if self.task_id:
   current=self.status()
   if current and current['state'] not in ('completed','cancelled'):raise ValueError('another dictation task is active')
  self.answer_pending=False;self.speaking=False;self.input_pending=False;self.child_ack=None;self.last_remote_sequence=0;self.remote_requested=False
  self.task_id=task_id;self.bound_owner=self.owner();self.bound_identity=self.identity_stamp();self.question=None;self.confirmation=None;self.pause_command=None
 def status(self):
  if not self.task_id:return None
  if self.owner()!=self.bound_owner or self.identity_stamp()!=self.bound_identity or self.service.privacy():self.confirmation=None;return None
  s=self.service.repository.status(self.task_id)
  if s['state'] in ('completed','cancelled'):
   self.question=None;self.pause_command=None;self.confirmation=None;self.child_ack=None
   self.answer_pending=False;self.speaking=False;self.remote_requested=False
  if 'actual' not in s:s['actual']={k:s.get(k) for k in ('phase_id','item_id','boot_id','session_id','action')}
  return s
 def stamp(self):
  s=self.status()
  if not s:return None
  a=s.get('actual',{})
  return (self.task_id,self.bound_owner,s['run_epoch'],s['state_revision'],a.get('phase_id'),a.get('item_id'),a.get('boot_id'),a.get('session_id'))
 def model_instructions(self):
  if self.closed:return ''
  s=self.status()
  if not s or s['state'] in ('completed','cancelled'):return ''
  task=self.service.repository.snapshot(self.task_id)
  skill=self.service.skills.get(task['skill_revision'])
  if not skill:raise ValueError('bound task skill revision is unavailable')
  return INSTRUCTIONS+'\n'+skill['content']+'\n当前任务固定的播报配置：'+json.dumps(task.get('playback',{}),ensure_ascii=False)
 def input_started(self):
  if self.task_id:self.input_pending=True;self.confirmation=None;self.remote_requested=False
 def observe_user(self,text,*,turn):
  self.input_pending=False
  self.turn=turn;self.confirmation=None;self.child_ack=None;self.remote_requested=False
  s=self.status()
  if not s or s['state'] in ('completed','cancelled','ready'):return
  value=re.sub(r'[\s，。！？,.!?]','',text)
  if s['state'] in ('paused','waiting_start') and value in ('继续','继续吧','可以继续','好了继续','恢复','开始','开始吧','开始默写','准备好了'):
   self.confirmation=(self.stamp(),turn);return
  if s['state']=='waiting_child' and value in ('写好了','我写好了','写完了','我写完了','完成了','好了','下一个','下一词','下一个词','下一页','下一屏','看好了','看完了','翻页'):
   self.child_ack=(self.stamp(),turn);return
  if value in ('写好了','好了','下一个','下一页','下一词','下一个词','上一词','上一个','上一个词','上一屏','再读一次','再读一遍','重播','暂停','结束默写','停止默写'):return
  self.question=(self.stamp(),turn);self.pause_command=None;self.answer_pending=True
 def allow_output(self):
  if self.closed or self.service.privacy() or self.input_pending:return False
  if not self.task_id:return True
  s=self.status()
  if not s:return False
  if not self.question:return s['state'] not in ('playing','presenting')
  old,_=self.question;now=self.stamp()
  return bool(s['state']=='paused' and now and old[:3]==now[:3] and old[4:]==now[4:] and
              self._pause_applied())
 def _pause_applied(self):
  repo=self.service.repository
  if not hasattr(repo,'events'):
   return (self.pause_command is None and self.question[0][3]==self.stamp()[3] or
           self.status().get('actual',{}).get('command_id')==self.pause_command)
  events=repo.events(self.task_id)
  if self.pause_command:
   anchors=[e for e in events if e.get('command_id')==self.pause_command and e.get('action')=='paused']
   if not anchors:return False
   revision=anchors[-1]['state_revision']
  else:revision=self.question[0][3]
  return all(e['action']=='resume_requested' for e in events if e['state_revision']>revision)
 def answer_ticket(self):
  stamp=self.stamp()
  return (stamp[:3]+stamp[4:] if stamp else None,self.question[1] if self.question else None)
 def answer_started(self):
  if not self.allow_output():raise ValueError('television pause receipt required')
  self.speaking=True
  return self.answer_ticket()
 def answer_drained(self,text,*,turn,ticket=None):
  self.status()  # Retire terminal dialogue gates before considering late playback.
  if ticket is not None and ticket!=self.answer_ticket():return
  self.speaking=False
  if self.closed or not self.question or not self.allow_output():return
  stamp,qturn=self.question
  if turn!=qturn:return
  self.answer_pending=False
  if text:self.service.results.record_assistance(self.bound_owner,self.task_id,stamp[5],text,str(turn)+':'+hashlib.sha256(text.encode()).hexdigest()[:16])
 async def control(self,action,*,timeout=5):
  s=self.status()
  if self.closed or not s:return dict(status='unavailable')
  deadline=asyncio.get_running_loop().time()+timeout
  if action in ('confirm','previous','replay') and (self.speaking or self.answer_pending):return dict(status='answer_not_drained')
  if action=='confirm':
   if self.child_ack!=(self.stamp(),self.turn):return dict(status='child_confirmation_required')
   self.child_ack=None
  if action in ('resume','start'):
   if hasattr(self.service.repository,'events'):self.observe_tv_confirmation()
   if self.speaking or self.answer_pending:return dict(status='answer_not_drained')
   if self.confirmation!=(self.stamp(),self.turn):return dict(status='confirmation_required')
   self.confirmation=None;self.remote_requested=False
  transport=getattr(self.service,'transport',None)
  target=self.question[0] if action=='pause' and self.question else self.stamp()
  def same_item():
   now=self.stamp()
   return now and target and now[:3]+now[4:]==target[:3]+target[4:] and self.status()['state'] not in ('completed','cancelled')
  async def refresh():
   remaining=deadline-asyncio.get_running_loop().time()
   if remaining<=0:raise asyncio.TimeoutError()
   await asyncio.wait_for(transport.poll(self.task_id),remaining)
  if action=='pause' and transport:
   try:await refresh()
   except asyncio.TimeoutError:return dict(status='unknown',reason='pause_state_timeout')
   if self.closed or not same_item():return dict(status='context_changed')
   s=self.status()
  task=self.service.repository.snapshot(self.task_id)
  def build(state):
   a=state.get('actual',{})
   return dict(schema_version=2,command_id=uuid.uuid4().hex,task_id=self.task_id,content_hash=task['content_hash'],run_epoch=state['run_epoch'],expected_state_revision=state['state_revision'],phase_id=a.get('phase_id'),item_id=a.get('item_id'),action=action)
  command=build(s);superseded=[]
  for attempt in range(2):
   if action=='pause':self.pause_command=command['command_id']
   try:
    remaining=deadline-asyncio.get_running_loop().time()
    if remaining<=0:raise asyncio.TimeoutError()
    result=await asyncio.wait_for(self.service.control_task(command,owner_id=self.bound_owner),remaining)
    break
   except asyncio.TimeoutError:return dict(status='unknown',command_id=command['command_id'],reason='control_timeout')
   except httpx.HTTPStatusError as exc:
    # A new revision needs a new command, never a changed retry payload. Only
    # pause may follow audio-only progress on the same bound item and boot.
    if action!='pause' or attempt or not transport or exc.response.status_code!=409:raise
    try:await refresh()
    except asyncio.TimeoutError:return dict(status='unknown',command_id=command['command_id'],reason='pause_state_timeout',superseded_command_ids=superseded)
    actual=self.service.repository.request_command(command)
    if actual['status']=='applied':
     result=actual
     break
    latest=self.status()
    if not latest or latest['state_revision']<=command['expected_state_revision']:raise
    self.service.repository.reject_command(command,reason='stale_revision')
    superseded.append(command['command_id'])
    if self.closed or not same_item():return dict(status='context_changed',superseded_command_ids=superseded)
    command=build(self.status())
  while result.get('status')=='pending' and not self.closed and asyncio.get_running_loop().time()<deadline:
   await asyncio.sleep(.025)
   if transport:
    try:await refresh()
    except asyncio.TimeoutError:break
    result=self.service.repository.request_command(command)
  if self.closed:return dict(status='cancelled')
  if action=='resume' and result.get('status')=='applied':self.question=None
  if superseded:result=dict(result,superseded_command_ids=superseded)
  return result
 def observe_tv_confirmation(self):
  s=self.status()
  if not s or s['state']!='paused':return
  for e in self.service.repository.events(self.task_id):
   if e.get('action')=='resume_requested' and e['sequence']>self.last_remote_sequence:
    self.last_remote_sequence=e['sequence']
    if e['state_revision']==s['state_revision'] and e.get('boot_id')==s['actual'].get('boot_id'):
     self.confirmation=(self.stamp(),self.turn);self.remote_requested=True
 def close(self):self.closed=True;self.confirmation=None;self.question=None

class DictationTools:
 def __init__(self,service,owner,session_id,*,device_id='unpaired',callback_id='local',identity_stamp=None):
  self.service,self.owner=service,owner
  self._source_session=session_id;self._input_session=None;self._input_binding=None
  self.session_id=lambda:self._input_session if self._input_binding and self._input_binding[0]==self._source_session() else self._source_session()
  self.device_id,self.callback_id=device_id,callback_id
  self.coordinator=DictationCoordinator(service,owner,identity_stamp);self.worker=None
 def register(self,registry):
  for definition in self.service.tool_definitions:
   spec=definition['function'];name=spec['name']
   async def handler(args,_name=name):return await self.call(_name,args)
   registry.register(name,spec['description'],spec['parameters'],handler)
  for name,desc,props,required in [
   ('deliver_dictation_task','发送已准备任务到电视，离线保留等待。',{'task_id':{'type':'string'}},['task_id']),
   ('get_dictation_task_status','读取当前真实电视状态。',{},[]),
   ('control_dictation_task','控制电视；暂停必须等实际回执，恢复校验真实确认及回答排空。',{'action':{'type':'string','enum':['start','confirm','pause','resume','replay','previous','cancel']}},['action'])]:
   async def handler(args,_name=name):return await self.call(_name,args)
   registry.register(name,desc,dict(type='object',properties=props,required=required,additionalProperties=False),handler)
 async def call(self,name,args):
  owner=self.owner()
  if owner is None or self.service.privacy():return dict(status='identity_required')
  if name in ('get_dictation_context','prepare_dictation_task'):
   result=await self.service.handle_tool(name,args,owner_id=owner,session_id=self.session_id(),paired_device_id=self.device_id,callback_registration_id=self.callback_id)
   if result.get('status')=='ready' and 'task' in result:self.coordinator.bind(result['task']['task_id'])
   return result
  if name=='deliver_dictation_task':
   self.service.results._task(owner,args['task_id']);self.coordinator.bind(args['task_id'])
   result=await self.service.deliver_task(args['task_id'],owner_id=owner)
   self.start_worker();return result
  if name=='get_dictation_task_status':
   state=self.coordinator.status()
   if not state:return dict(status='unavailable')
   task=self.service.repository.snapshot(state['task_id'])
   state['skill']=self.service.skills.get(task['skill_revision'])
   state['playback']=task.get('playback',{})
   state['current_item']=next((dict(i,phase_type=p['type']) for p in task['phases'] for i in p['items'] if i['item_id']==state['actual'].get('item_id')),None)
   return state
  if name=='control_dictation_task':return await self.coordinator.control(args['action'])
  raise ValueError('unknown dictation tool')
 def adopt(self,task_id):
  owner=self.owner()
  if owner is None or self.service.privacy():return dict(status='identity_required')
  self.service.results._task(owner,task_id)
  registry=getattr(self.service,'_task_workers',None)
  if registry is None:registry={};self.service._task_workers=registry
  previous=registry.get(task_id)
  if previous is not None and previous is not self:
   # Preserve an in-flight explanation gate when another trusted surface adopts.
   if previous.coordinator.question or previous.coordinator.speaking or previous.coordinator.answer_pending:
    return dict(status='dialogue_active',task_id=task_id)
   previous.coordinator.close()
   if previous.worker:previous.worker.cancel()
  registry[task_id]=self
  c=self.coordinator
  if c.closed or c.task_id==task_id and c.bound_identity!=c.identity_stamp():c.task_id=None
  c.closed=False;c.bind(task_id);self.start_worker()
  return dict(status='adopted',task_id=task_id)
 def adopt_active(self):
  owner=self.owner()
  if owner is None or self.service.privacy():return dict(status='identity_required')
  candidates=[]
  tables={r[0] for r in self.service.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  for state in self.service.repository.list_tasks(owner):
   if state['revoked'] or state['state'] in ('completed','cancelled'):continue
   task=self.service.repository.snapshot(state['task_id'])
   if not task or task['schema_version']!=2:continue
   delivered=state['state']!='ready'
   if not delivered and 'dictation_delivery' in tables:
    delivered=bool(self.service.db.execute('SELECT 1 FROM dictation_delivery WHERE task_id=?',(state['task_id'],)).fetchone())
   if delivered:candidates.append(state['task_id'])
  if len(candidates)!=1:return dict(status='ambiguous_tasks' if candidates else 'no_active_task')
  return self.adopt(candidates[0])
 def start_worker(self):
  if self.worker is None or self.worker.done():self.worker=asyncio.create_task(self._run())
 async def _run(self):
  while not self.coordinator.closed:
   try:
    s=self.coordinator.status()
    if not s or s['state'] in ('completed','cancelled'):return
    t=getattr(self.service,'transport',None)
    if t:
     if t.status(s['task_id'])['delivery_state'] in ('waiting_tv','not_delivered','awaiting_receipt'):
      await self.service.deliver_task(s['task_id'],owner_id=self.coordinator.bound_owner)
     await t.poll(s['task_id'])
     s=self.coordinator.status()
     if not s or s['state'] in ('completed','cancelled'):return
     await t.lease(s['task_id'],6000)
     if hasattr(self.service,'sync_display'):await self.service.sync_display()
     c=self.coordinator;c.observe_tv_confirmation()
     if c.remote_requested and c.confirmation and not c.answer_pending and not c.speaking:
      await c.control('resume')
   except (OSError,ValueError,RuntimeError):pass
   except Exception:pass  # Network failure must leave the TV lease to expire paused.
   await asyncio.sleep(2)
 def event(self,kind,text):
  if kind=='input_started':self.coordinator.input_started()
  if kind=='user_text':
   if self.owner() is not None:
    if hasattr(self.service,'begin_session'):
     binding=(self._source_session(),self.owner(),self.service.owner_epoch(self.owner()))
     if self._input_binding!=binding:
      self._input_session='voice-'+uuid.uuid4().hex;self._input_binding=binding
     self.service.begin_session(self.owner(),self.session_id(),turn='voice:'+str(self.coordinator.turn+1))
    self.service.observe_initiator(self.owner(),self.session_id(),text,turn='voice:'+str(self.coordinator.turn+1))
   state=self.coordinator.status()
   if state is None or state['state'] in ('completed','cancelled'):self.adopt_active()
   self.coordinator.observe_user(text,turn=self.coordinator.turn+1)

 async def close(self):
  try:
   c=self.coordinator
   status=self.service.repository.status(c.task_id) if c.task_id else None
   if status and status['state'] not in ('paused','ready','waiting_start','completed','cancelled'):
    task=self.service.repository.snapshot(c.task_id)
    command=dict(schema_version=2,command_id=uuid.uuid4().hex,task_id=c.task_id,content_hash=task['content_hash'],run_epoch=status['run_epoch'],expected_state_revision=status['state_revision'],phase_id=status.get('phase_id'),item_id=status.get('item_id'),action='pause')
    await asyncio.wait_for(self.service.control_task(command,owner_id=c.bound_owner),.5)
  except Exception:pass

  self.coordinator.close()
  if self.worker and self.worker is not asyncio.current_task():
   self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)


class DictationDialogueAgent:
 """Bounded real-model dialogue using the same registered production handlers.

 The caller supplies actual device playback and reports its drain. A text-only
 harness may inspect text but cannot authorize TV resume as if audio drained.
 """
 def __init__(self,tools,client,model,*,speak=None,max_calls=8,request_options=None):
  self.tools,self.client,self.model,self.speak,self.max_calls=tools,client,model,speak,max_calls
  self.request_options=request_options or {}
  task=tools.service.repository.snapshot(tools.coordinator.task_id) if tools.coordinator.task_id else None
  skill=tools.service.skills.get(task['skill_revision'] if task else None)['content']
  self.messages=[dict(role='system',content=INSTRUCTIONS+'\n'+skill+'\n当前任务固定的播报配置：'+json.dumps(task.get('playback',{}) if task else {},ensure_ascii=False)+'\n提问时先查询当前项，暂停回执后解释，切勿重新准备任务。')]
  self.trace=[]
 async def turn(self,text,*,turn):
  c=self.tools.coordinator;c.observe_user(text,turn=turn)
  self.messages.append(dict(role='user',content=text))
  from .realtime_capabilities import CapabilityRegistry
  registry=CapabilityRegistry();self.tools.register(registry)
  registry.entries={k:v for k,v in registry.entries.items() if k in ('get_dictation_task_status','control_dictation_task')}
  trace=[]
  definitions=[dict(type='function',function=e.spec | {}) for e in registry.entries.values()]
  for d in definitions:d['function'].pop('type',None)
  for _ in range(self.max_calls):
   response=await self.client.chat.completions.create(model=self.model,messages=self.messages,tools=definitions,**self.request_options)
   message=response.choices[0].message
   calls=getattr(message,'tool_calls',None)
   self.messages.append(message.model_dump(exclude_none=True))
   if calls:
    import json
    for call in calls:
     name=call.function.name
     if name not in registry.entries:raise ValueError('unknown dictation tool')
     args=json.loads(call.function.arguments);registry.validate(name,args)
     try:result=await registry.entries[name].handler(args)
     except (ValueError,TypeError) as exc:result=dict(status='error',reason=str(exc))
     record=dict(tool=name,arguments=args,result=result);trace.append(record);self.trace.append(record)
     self.messages.append(dict(role='tool',tool_call_id=call.id,content=json.dumps(result,ensure_ascii=False)))
    continue
   answer=message.content or ''
   if not c.allow_output():
    if any(r['tool']=='control_dictation_task' and r['arguments'].get('action') in ('start','resume','confirm','replay','previous') and r['result'].get('status')=='applied' for r in trace):return dict(status='controlled',text='',trace=trace)
    return dict(status='output_blocked',text='',reason='actual_pause_receipt_required',trace=trace)
   if self.speak and answer:
    ticket=c.answer_started()
    if await self.speak(answer):c.answer_drained(answer,turn=turn,ticket=ticket)
    else:return dict(status='interrupted',text=answer,trace=trace)
   return dict(status='answered',text=answer,trace=trace)
  return dict(status='tool_limit',trace=trace)
