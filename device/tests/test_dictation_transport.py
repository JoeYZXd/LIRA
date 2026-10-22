import json, sqlite3
from pathlib import Path
import httpx, pytest
from lira.dictation_repository import DictationRepository, RepositoryError
from lira.dictation_transport import DictationTransport

def fixture():
 t=json.loads((Path(__file__).parents[2]/'tv-receiver/tests/fixtures/dictation-v2.json').read_text(encoding='utf8'))
 db=sqlite3.connect(':memory:');r=DictationRepository(db);r.submit(t,paired_device_id='tv',owner_id=1)
 return db,r,t

def event(t,n,action='task_received',state='waiting_start',**kw):
 return dict(schema_version=2,task_id=t['task_id'],content_hash=t['content_hash'],event_id=str(n),run_epoch=1,sequence=n,state_revision=n,command_id=None,phase_id=None,item_id=None,action=action,state=state,actual_repeats=0,boot_id='b',session_id='s',occurred_at='now')|kw

@pytest.mark.asyncio
async def test_receipt_committed_before_ack_and_lost_ack_retries(tmp_path):
 db,r,t=fixture();calls=[];fail=True
 def handle(req):
  nonlocal fail
  assert req.headers['authorization']=='Bearer secret'
  body=json.loads(req.content) if req.content else {};calls.append((req.url.path,body))
  if req.url.path=='/dictation/events':return httpx.Response(200,json={'events':[event(t,1)]})
  if req.url.path=='/dictation/ack':
   assert not db.in_transaction
   assert r.status(t['task_id'])['last_sequence']==1
   if fail:fail=False;raise httpx.ConnectError('lost ack')
  return httpx.Response(200,json={'status':'ok'})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
  x=DictationTransport(r,c,base='http://tv',token='secret',device_id='tv')
  with pytest.raises(httpx.ConnectError):await x.poll(t['task_id'])
  await x.poll(t['task_id'])
 assert len(r.events(t['task_id']))==1
 assert calls[-1][1]['through_sequence']==1

@pytest.mark.asyncio
async def test_offline_delivery_persists_retry_same_task(tmp_path):
 db,r,t=fixture();online=False;received=[]
 # No upload synthesis needed: recognition-only frozen task.
 from lira.dictation_task_contract import content_hash
 t['task_id']='recognition-only';t['phases']=[p for p in t['phases'] if p['type']=='recognition'];t['audio_assets']=[];t['content_hash']=content_hash(t)
 r.submit(t,paired_device_id='tv',owner_id=1)
 def handle(req):
  if not online:raise httpx.ConnectError('offline')
  if req.url.path=='/status':return httpx.Response(200,json={'capabilities':['dictation_task_v2']})
  if req.url.path=='/dictation/events':return httpx.Response(200,json={'events':[event(t,1)]})
  if req.url.path=='/dictation/start':received.append(json.loads(req.content))
  return httpx.Response(200,json={'status':'ok'})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
  x=DictationTransport(r,c,base='http://tv',token='secret',device_id='tv')
  p={'task':t,'manifest':dict(task_id=t['task_id'],content_hash=t['content_hash'],assets=[]),'folder':str(tmp_path)}
  assert (await x.deliver(p))['delivery_state']=='waiting_tv'
  online=True;assert (await x.deliver(p))['state']=='waiting_start'
  await x.deliver(p)
 assert received[0]==received[1] and len(r.events(t['task_id']))==1

def test_completion_requires_confirmations_and_replay_count_is_cumulative():
 db,r,t=fixture()
 with pytest.raises(RepositoryError):r.receive(event(t,1,'task_completed','completed'),paired_device_id='tv')
 r.receive(event(t,1,'playback_completed','waiting_child',actual_repeats=8),paired_device_id='tv')
 n=1
 for p in t['phases']:
  for item in p['items']:
   n+=1;r.receive(event(t,n,'item_confirmed','waiting_child',phase_id=p['phase_id'],item_id=item['item_id']),paired_device_id='tv')
 n+=1;r.receive(event(t,n,'task_completed','completed'),paired_device_id='tv')
 assert r.counts(1)[0]['count']==1
 assert r.word_history(1,t['lessons'][0]['source']['book_id'],t['lessons'][0]['source']['unit'],t['lessons'][0]['source']['number'])

@pytest.mark.asyncio
async def test_actual_http_service_pause_is_pending_until_persisted_receipt():
 from fastapi import FastAPI, Request
 app=FastAPI();db,r,t=fixture();outbox=[event(t,1)];acks=[];emit=False
 @app.post('/dictation/events')
 async def events(request:Request):
  body=await request.json();assert body['limit']==256
  return {'events':[e for e in outbox if e['sequence']>body['after']]}
 @app.post('/dictation/ack')
 async def ack(request:Request):acks.append(await request.json());return {'status':'ok'}
 @app.post('/dictation/control')
 async def control(request:Request):
  c=await request.json()
  if emit:outbox.append(event(t,2,'paused','paused',command_id=c['command_id']))
  return {'state':'paused'} # A claimed HTTP state is insufficient.
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
  x=DictationTransport(r,c,base='http://tv',token='secret',device_id='tv');await x.poll(t['task_id'])
  cmd=dict(schema_version=2,command_id='pause',task_id=t['task_id'],content_hash=t['content_hash'],run_epoch=1,expected_state_revision=1,phase_id=None,item_id=None,action='pause')
  assert (await x.command(cmd))['status']=='pending'
  assert r.status(t['task_id'])['state']=='waiting_start'
  emit=True;assert (await x.command(cmd))['status']=='applied'
  assert acks[-1]['through_sequence']==2

@pytest.mark.asyncio
async def test_gap_requests_same_contiguous_cursor_and_conflict_never_acks():
 db,r,t=fixture();batches=[[event(t,2,'paused','paused')],[event(t,1),event(t,2,'paused','paused')],[event(t,2,'resumed','playing')]];cursors=[];acks=[]
 def handle(req):
  body=json.loads(req.content)
  if req.url.path.endswith('/events'):cursors.append(body['after']);return httpx.Response(200,json={'events':batches.pop(0)})
  acks.append(body['through_sequence']);return httpx.Response(200,json={'status':'ok'})
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
  x=DictationTransport(r,c,base='http://tv',token='secret',device_id='tv')
  await x.poll(t['task_id']);await x.poll(t['task_id'])
  with pytest.raises(RepositoryError):await x.poll(t['task_id'])
 assert cursors==[0,0,2] and acks==[0,2]


@pytest.mark.asyncio
async def test_manifest_tampering_rejected_before_network():
 db,r,t=fixture()
 class Never:
  async def get(self,*a,**k):raise AssertionError('network reached')
 x=DictationTransport(r,Never(),base='http://tv',token='secret',device_id='tv')
 with pytest.raises(ValueError,match='manifest scope'):
  await x.deliver(dict(task=t,manifest=dict(task_id='wrong',content_hash=t['content_hash'],assets=[]),folder='.'))
 with pytest.raises(ValueError,match='manifest asset'):
  await x.deliver(dict(task=t,manifest=dict(task_id=t['task_id'],content_hash=t['content_hash'],assets=[]),folder='.'))
