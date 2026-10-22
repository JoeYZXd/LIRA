import sqlite3,json
import httpx,pytest
from lira.dictation_settings import DictationSettings
from test_dictation_service import service
from test_dictation_routes import setup
from test_dev_console import login

def test_display_default_off_strict_validation_and_cas():
 settings=DictationSettings(sqlite3.connect(':memory:'))
 assert settings.get_display()==dict(revision=0,debug_enabled=False)
 assert settings.save_display(True,expected_revision=0)==dict(revision=1,debug_enabled=True)
 with pytest.raises(ValueError):settings.save_display(False,expected_revision=0)
 for invalid in [1,0,'false',None]:
  with pytest.raises(ValueError):settings.save_display(invalid,expected_revision=1)
 assert settings.save_display(False,expected_revision=1)['debug_enabled'] is False

@pytest.mark.asyncio
async def test_live_display_configuration_has_real_receiver_ack(service):
 calls=[]
 def handle(req):
  calls.append((req.url.path,json.loads(req.content) if req.content else None))
  if req.url.path=='/status':return httpx.Response(200,json=dict(capabilities=['dictation_display_settings_v1']))
  assert req.headers['Authorization']=='Bearer test-token'
  return httpx.Response(200,json=json.loads(req.content))
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
  service.attach_transport(client,base='http://tv',token='test-token',device_id='tv')
  service.settings.save_display(True,expected_revision=0)
  assert (await service.sync_display())==dict(status='applied',debug_enabled=True)
  service.settings.save_display(False,expected_revision=1)
  assert (await service.sync_display())['debug_enabled'] is False
 assert calls[-1]==('/dictation/display',{'debug_enabled':False})

@pytest.mark.asyncio
async def test_offline_display_saved_and_retried_without_changing_task_config(service):
 def offline(req):raise httpx.ConnectError('unavailable')
 old=service.settings.get_v2()
 async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
  service.attach_transport(client,base='http://tv',token='test-token',device_id='tv')
  service.settings.save_display(True,expected_revision=0)
  assert (await service.sync_display())['status']=='pending'
 assert service.settings.get_display()['debug_enabled'] is True
 assert service.settings.get_v2()==old

def test_parent_display_api_permissions_conflict_and_offline_receipt(setup):
 c,rt,s=setup
 assert c.post('/dev/api/dictation/display',json={'debug_enabled':True,'expected_revision':0}).status_code==401
 login(c)
 assert c.get('/dev/api/dictation').json()['display']['debug_enabled'] is False
 payload=dict(debug_enabled=True,expected_revision=0)
 assert c.post('/dev/api/dictation/display',json=payload,headers={'Origin':'https://evil.test'}).status_code==403
 response=c.post('/dev/api/dictation/display',json=payload);assert response.status_code==200,response.text
 assert response.json()==dict(display=dict(revision=1,debug_enabled=True),television=dict(status='pending'))
 assert c.post('/dev/api/dictation/display',json=payload).status_code==409
 assert c.get('/dev/api/dictation').json()['display']['debug_enabled'] is True

@pytest.mark.asyncio
async def test_display_ack_cached_until_receiver_restart(service):
 instance='first';posts=[]
 def handle(req):
  if req.url.path=='/status':return httpx.Response(200,json=dict(instance=instance,capabilities=['dictation_display_settings_v1']))
  posts.append(json.loads(req.content));return httpx.Response(200,json=posts[-1])
 async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
  service.attach_transport(client,base='http://tv',token='test-token',device_id='tv')
  await service.sync_display();await service.sync_display();assert len(posts)==1
  instance='restarted';await service.sync_display();assert len(posts)==2
