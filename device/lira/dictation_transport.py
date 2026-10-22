"""Authenticated, retryable v2 delivery; only committed TV receipts advance state."""
import asyncio
import httpx
from .dictation_assets import DictationAssets
from .db_atomic import atomic

class DictationTransport:
 def __init__(self,repository,client,*,base,token,device_id):
  self.repository,self.db,self.client=repository,repository.db,client
  self.base,self.token,self.device_id=base.rstrip('/'),token,device_id
  self.lock=asyncio.Lock()
  self.db.execute('CREATE TABLE IF NOT EXISTS dictation_delivery(task_id TEXT PRIMARY KEY,state TEXT NOT NULL)')
 def _scope(self,tid):
  t=self.repository._task(tid)
  if t['device']!=self.device_id or t['revoked']:raise ValueError('unauthorized task')
  return dict(task_id=tid,content_hash=t['digest'],run_epoch=t['run_epoch'])
 async def _post(self,path,payload):
  r=await self.client.post(self.base+path,json=payload,headers={'Authorization':'Bearer '+self.token});r.raise_for_status();return r.json()
 def status(self,tid):
  self._scope(tid);row=self.db.execute('SELECT state FROM dictation_delivery WHERE task_id=?',(tid,)).fetchone()
  return dict(self.repository.status(tid),delivery_state=row[0] if row else 'not_delivered')
 def _delivery(self,tid,state):
  with atomic(self.db):self.db.execute('INSERT OR REPLACE INTO dictation_delivery VALUES(?,?)',(tid,state))
 async def configure_display(self,settings):
  r=await self.client.get(self.base+'/status',headers={'Authorization':'Bearer '+self.token});r.raise_for_status()
  if 'dictation_display_settings_v1' not in r.json().get('capabilities',[]):return dict(status='upgrade_required')
  stamp=(r.json().get('instance'),settings['debug_enabled'])
  if stamp[0] and getattr(self,'_display_ack',None)==stamp:return dict(status='applied',debug_enabled=settings['debug_enabled'])
  result=await self._post('/dictation/display',dict(debug_enabled=settings['debug_enabled']))
  if result.get('debug_enabled') is not settings['debug_enabled']:raise ValueError('display setting acknowledgement mismatch')
  self._display_ack=stamp
  return dict(status='applied',debug_enabled=settings['debug_enabled'])
 async def deliver(self,prepared):
  async with self.lock:
   tid=prepared['task']['task_id'];scope=self._scope(tid)
   if self.repository.snapshot(tid)!=prepared['task']:raise ValueError('task snapshot mismatch')
   manifest=prepared['manifest']
   if any(manifest.get(k)!=prepared['task'][k] for k in ('task_id','content_hash')):raise ValueError('manifest scope mismatch')
   required={a['asset_id']:a['sha256'] for a in prepared['task']['audio_assets']}
   actual=manifest.get('assets')
   if type(actual) is not list or len(actual)!=len(required) or {a.get('asset_id'):a.get('sha256') for a in actual}!=required:raise ValueError('manifest asset mismatch')
   self._delivery(tid,'waiting_tv')
   try:
    r=await self.client.get(self.base+'/status',headers={'Authorization':'Bearer '+self.token});r.raise_for_status()
    caps=r.json().get('capabilities',[])
    if 'dictation_task_v2' not in caps:raise ValueError('v2 capability required')
    if any(p['type']=='recognition' and any(len(i['text'])>1 for i in p['items']) for p in prepared['task']['phases']) and 'dictation_recognition_words_v1' not in caps:raise ValueError('recognition words capability required; upgrade TV')
    if prepared['task']['playback'].get('manual_navigation') and 'dictation_manual_navigation_v1' not in caps:raise ValueError('manual navigation capability required; upgrade TV')
    await DictationAssets('.',None).upload(prepared['manifest'],prepared['folder'],self.client,base=self.base,token=self.token)
    await self._post('/dictation/start',dict(task=prepared['task'],manifest=prepared['manifest'],run_epoch=scope['run_epoch']))
    await self.poll(tid)
    self._delivery(tid,'received' if self.repository.status(tid)['last_sequence'] else 'awaiting_receipt')
   except (httpx.TransportError,httpx.HTTPStatusError):pass
   return self.status(tid)
 async def poll(self,tid):
  # A surrounding application transaction cannot be acknowledged as durable.
  if self.db.in_transaction:raise RuntimeError('receipt polling requires committed database')
  scope=self._scope(tid);before=self.repository.status(tid)['last_sequence']
  response=await self._post('/dictation/events',dict(scope,after=before,limit=256))
  events=response.get('events')
  if type(events) is not list or len(events)>256:raise ValueError('invalid event batch')
  for event in events:
   if event.get('task_id')!=tid:raise ValueError('cross-task event')
   self.repository.receive(event,paired_device_id=self.device_id)
  through=self.repository.status(tid)['last_sequence']
  await self._post('/dictation/ack',dict(scope,through_sequence=through))
  return self.repository.status(tid)
 async def command(self,command):
  async with self.lock:
   self._scope(command['task_id']);result=self.repository.request_command(command)
   if result['status']!='pending':return result
   await self._post('/dictation/control',command)
   await self.poll(command['task_id'])
   return self.repository.request_command(command)
 async def lease(self,tid,lease_ms=10000):
  if type(lease_ms) is not int or not 1000<=lease_ms<=15000:raise ValueError('invalid lease')
  return await self._post('/dictation/lease',dict(self._scope(tid),lease_ms=lease_ms))
