"""TV protocol endpoint only. Business tasks and knowledge remain in production code."""
import asyncio
import base64
import hashlib
import json
import secrets
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, Response
import uvicorn

class TVServer:
    def __init__(self):
        self.app=FastAPI();self.token=secrets.token_urlsafe(24);self.online=False
        self.disconnected=False;self.events=[];self.pending=[];self.uploads={};self.assets={}
        self.snapshot=None;self.task=None;self.epoch=None;self.ack=0;self.requests=[];self.serial=0
        self._mount()

    async def dispatch(self,action,payload):
        self.serial+=1;f=asyncio.get_running_loop().create_future()
        row=dict(id=self.serial,action=action,payload=payload,future=f);self.pending.append(row)
        try:return await asyncio.wait_for(f,8)
        finally:
            if row in self.pending:self.pending.remove(row)

    def _mount(self):
        a=self.app;root=Path(__file__).parent
        async def auth(request):
            if request.headers.get('authorization')!='Bearer '+self.token:raise HTTPException(401)
            if not self.online or self.disconnected:raise HTTPException(503,'simulated TV unavailable')
        @a.get('/')
        async def page():return FileResponse(root/'index.html')
        @a.get('/tv-simulator.js')
        async def script():return FileResponse(root/'tv-simulator.js',media_type='application/javascript')
        @a.get('/sim/poll')
        async def poll():
            return dict(commands=[{k:r[k] for k in ('id','action','payload')} for r in self.pending if not r['future'].done()])
        @a.post('/sim/reply/{number}')
        async def reply(number:int,request:Request):
            data=await request.json()
            for r in self.pending:
                if r['id']==number and not r['future'].done():r['future'].set_result(data)
            return dict(ok=True)
        @a.post('/sim/event')
        async def event(request:Request):
            row=await request.json()
            if self.task and (row['task_id']!=self.task['task_id'] or row['content_hash']!=self.task['content_hash'] or row['run_epoch']!=self.epoch):raise HTTPException(409)
            if row['sequence']<=len(self.events):
                if self.events[row['sequence']-1]==row:return dict(ok=True,duplicate=True)
                raise HTTPException(409,'conflicting durable event')
            if row['sequence']!=len(self.events)+1:raise HTTPException(409,'noncontiguous event')
            self.events.append(row);self.snapshot=row
            return dict(ok=True)
        @a.get('/sim/asset/{aid}')
        async def asset(aid:str):
            key=(self.task['task_id'],self.task['content_hash'],aid) if self.task else None
            if key not in self.assets:raise HTTPException(404)
            return Response(self.assets[key],media_type='application/octet-stream')
        @a.get('/status')
        async def status(request:Request):
            await auth(request)
            return dict(capabilities=['dictation_task_v2','dictation_recognition_words_v1'],interactive=True,foreground=True)
        @a.get('/dictation/status')
        async def task_status(request:Request):
            await auth(request)
            return dict(state=self.snapshot,task_id=self.task['task_id'] if self.task else None)
        @a.post('/dictation/assets/{operation}')
        async def assets(operation:str,request:Request):
            await auth(request)
            try:p=await request.json();key=(p['task_id'],p['content_hash'],p['asset_id'])
            except (ValueError,TypeError,KeyError):raise HTTPException(400,'invalid asset envelope')
            if any(type(v) is not str for v in key) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',key[0]) or not re.fullmatch('[0-9a-f]{64}',key[1]) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',key[2]) or '..' in key[0] or '..' in key[2]:raise HTTPException(400,'invalid asset scope')
            if operation=='start':
                if set(p)!={'task_id','content_hash','asset_id','sha256','pcm_bytes','frames','sample_rate','channels'}:raise HTTPException(400,'invalid descriptor')
                if type(p['pcm_bytes']) is not int or not 0<p['pcm_bytes']<=5760000 or p['pcm_bytes']%2 or type(p['frames']) is not int or p['frames']*2!=p['pcm_bytes'] or type(p['sample_rate']) is not int or p['sample_rate']!=24000 or type(p['channels']) is not int or p['channels']!=1 or type(p['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',p['sha256']):raise HTTPException(400,'invalid PCM descriptor')
                if key in self.assets:
                    if len(self.assets[key])!=p['pcm_bytes'] or hashlib.sha256(self.assets[key]).hexdigest()!=p['sha256']:raise HTTPException(409,'committed asset conflict')
                    return dict(ok=True,ready=True)
                if key in self.uploads:
                    if self.uploads[key]['meta']!=p:raise HTTPException(409,'asset reservation conflict')
                    return dict(ok=True)
                if sum(len(v) for v in self.assets.values())+sum(r['meta']['pcm_bytes'] for r in self.uploads.values())+p['pcm_bytes']>64*1024*1024:raise HTTPException(413,'task audio quota')
                self.uploads[key]=dict(meta=p,data=bytearray())
            elif operation=='chunk':
                if key not in self.uploads:raise HTTPException(409,'asset not reserved in this scope')
                row=self.uploads[key]
                if set(p)!={'task_id','content_hash','asset_id','offset','pcm_base64'} or type(p.get('offset')) is not int or p['offset']!=len(row['data']):raise HTTPException(409,'invalid chunk offset')
                try:raw=base64.b64decode(p['pcm_base64'],validate=True)
                except (ValueError,TypeError):raise HTTPException(400,'invalid chunk')
                if not raw or len(raw)>24000 or len(row['data'])+len(raw)>row['meta']['pcm_bytes']:raise HTTPException(413,'chunk quota')
                row['data'].extend(raw)
            elif operation=='commit':
                if key not in self.uploads:
                    if key in self.assets:return dict(ok=True)
                    raise HTTPException(409,'asset not reserved')
                row=self.uploads[key];raw=bytes(row['data']);meta=row['meta']
                if len(raw)!=meta['pcm_bytes'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise HTTPException(400)
                self.assets[key]=raw;del self.uploads[key]
            else:raise HTTPException(404)
            return dict(ok=True)
        @a.post('/dictation/{operation}')
        async def protocol(operation:str,request:Request):
            await auth(request);p=await request.json();self.requests.append(dict(operation=operation,payload=p))
            if operation=='start':
                try:
                    from lira.dictation_task_contract import validate_task
                    task=validate_task(p['task']);manifest=p['manifest']
                    if task['schema_version']!=2 or type(p['run_epoch']) is not int or p['run_epoch']<1:raise ValueError('invalid v2 task epoch')
                    if set(manifest)!={'task_id','content_hash','assets'} or manifest['task_id']!=task['task_id'] or manifest['content_hash']!=task['content_hash']:raise ValueError('manifest scope')
                    required={v['asset_id']:v['sha256'] for v in task['audio_assets']}
                    if type(manifest['assets']) is not list or len(manifest['assets'])!=len(required) or {v['asset_id']:v['sha256'] for v in manifest['assets']}!=required:raise ValueError('manifest coverage')
                    for descriptor in manifest['assets']:
                        if set(descriptor)!={'asset_id','sha256','pcm_bytes','frames','sample_rate','channels'}:raise ValueError('manifest descriptor')
                        raw=self.assets.get((task['task_id'],task['content_hash'],descriptor['asset_id']))
                        if raw is None or descriptor['pcm_bytes']!=len(raw) or descriptor['frames']*2!=len(raw) or descriptor['sample_rate']!=24000 or descriptor['channels']!=1 or hashlib.sha256(raw).hexdigest()!=descriptor['sha256']:raise ValueError('uncommitted or inconsistent audio')
                except (ValueError,TypeError,KeyError):raise HTTPException(400,'invalid immutable task or audio manifest')
                if self.task:
                    if self.task!=p['task'] or self.epoch!=p['run_epoch']:raise HTTPException(409)
                    if self.snapshot:return dict(ok=True,state=self.snapshot['state'])
                    return await self.dispatch('start',p)
                self.task=p['task'];self.epoch=p['run_epoch'];return await self.dispatch('start',p)
            if not self.task or p.get('task_id')!=self.task['task_id'] or p.get('content_hash')!=self.task['content_hash'] or p.get('run_epoch')!=self.epoch:raise HTTPException(409)
            if operation=='events':return dict(events=self.events[p.get('after',0):p.get('after',0)+min(p.get('limit',256),256)])
            if operation=='ack':self.ack=p['through_sequence'];return dict(ok=True)
            if operation=='control':
                result=await self.dispatch('control',p)
                if result.get('ok') is not True:raise HTTPException(409,'television rejected stale or unavailable control')
                return result
            if operation=='lease':return await self.dispatch('lease',p)
            raise HTTPException(404)

    @asynccontextmanager
    async def serve(self):
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        config=uvicorn.Config(self.app,host='127.0.0.1',port=port,log_level='error',access_log=False)
        server=uvicorn.Server(config);job=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started:break
                if job.done():await job
                await asyncio.sleep(.02)
            if not server.started:raise RuntimeError('loopback server did not start')
            self.base=f'http://127.0.0.1:{port}'
            yield self
        finally:
            for r in self.pending:
                if not r['future'].done():r['future'].cancel()
            self.pending.clear();server.should_exit=True
            await asyncio.wait_for(job,10);sock.close()
