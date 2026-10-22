"""TC1: one explicit real-model run; no retries, looping, hardware, or production DB.

Programmatic entry: await run_once(client=..., model=..., synthesize=...,
run_id='unique', artifact_dir=Path(...), request_options={...}). The caller owns
the injected model/TTS resources. This module closes every resource it creates.
Normal pytest never invokes this entry against a real model.
"""
import argparse
import asyncio
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

DEVICE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(DEVICE));sys.path.insert(0,str(DEVICE/'tests'))
FIXTURE=DEVICE/'tests'/'fixtures'/'dictation-long-flow.json'

def load_fixture():
    f=json.loads(FIXTURE.read_text(encoding='utf8'))
    if f['max_seconds']!=900 or f['max_child_turns']!=60:raise ValueError('TC1 hard limits changed')
    if len(f['two'])!=18 or len(set(f['two']))!=18 or len(f['four'])!=12:raise ValueError('invalid coverage fixture')
    if any(not any(c in w for w in f['words']) for c in f['four']):raise ValueError('incomplete fixture words')
    return f

def claim_run(artifact_dir,run_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',run_id):raise ValueError('invalid run ID')
    root=Path(artifact_dir);root.mkdir(parents=True,exist_ok=True)
    run=root/run_id;run.mkdir(exist_ok=False)
    (run/'run.lock').write_text('TC1 claimed: never restart or retry this run ID\n',encoding='utf8')
    return run

def check_preparation_trace(first,prepared):
    # Asking which textbook to use is legitimate before a context lookup. The
    # lookup must still happen before the model submits its final preparation.
    names=[t['tool'] for result in (first,prepared) for t in result.get('trace',[])]
    if 'get_dictation_context' not in names or 'prepare_dictation_task' not in names or names.index('get_dictation_context')>names.index('prepare_dictation_task'):
        raise AssertionError('model must read actual context before preparation')

class RunRecord:
    def __init__(self,path,run_id,max_turns=60):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True);self.max_turns=max_turns;self.terminal=False
        self.data=dict(run_id=run_id,status='running',started_at=time.time(),evidence_type='real model + local H5; not physical TV proof',steps=[],model_calls=[],inputs=[],counters=dict(regression_runs_started=1,initial_requests=0,new_task_ids=0,task_completed=0,prepare_after_ready=0,child_turns=0),cleanup={})
    def save(self):
        temp=self.path/'report.json.tmp';temp.write_text(json.dumps(self.data,ensure_ascii=False,indent=2,default=str),encoding='utf8');temp.replace(self.path/'report.json')
    def step(self,name,**evidence):
        if self.terminal:raise RuntimeError('run is terminal')
        self.data['steps'].append(dict(name=name,status='passed',at=time.time(),**evidence));self.save()
    def child_turn(self,text):
        if self.terminal:raise RuntimeError('run is terminal')
        if self.data['counters']['child_turns']>=self.max_turns:raise RuntimeError('child turn budget exhausted')
        self.data['counters']['child_turns']+=1
        self.data['inputs'].append(dict(text=text,turn=self.data['counters']['child_turns'],at=time.time()))
        return self.data['counters']['child_turns']
    def finish(self,status,**details):
        self.terminal=True
        if status in ('failed','timeout'):
            self.data['steps'].append(dict(name='terminal_failure',status=status,at=time.time(),after=self.data['steps'][-1]['name'] if self.data['steps'] else 'preflight',reason=details.get('reason','')))
        self.data.update(status=status,finished_at=time.time(),**details);self.save()

class AuditedClient:
    """Observe actual SDK results; never substitute messages or tool results."""
    def __init__(self,client,record):
        self.client,self.record=client,record
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
    async def create(self,**kwargs):
        call=dict(number=len(self.record.data['model_calls'])+1,model=kwargs.get('model'),messages=copy.deepcopy(kwargs['messages']),started_at=time.time())
        self.record.data['model_calls'].append(call);self.record.save()
        response=await self.client.chat.completions.create(**kwargs)
        call.update(response_id=getattr(response,'id',None),finished_at=time.time(),response=response.model_dump(exclude_none=True) if hasattr(response,'model_dump') else str(response))
        self.record.save();return response

async def preflight(*,channel='msedge'):
    """Offline browser/audio prerequisite only; no model or task preparation."""
    from playwright.async_api import async_playwright
    from h5_tv.server import TVServer
    server=TVServer()
    async with server.serve(),async_playwright() as p:
        browser=await p.chromium.launch(channel=channel,headless=True,args=['--autoplay-policy=no-user-gesture-required'])
        try:
            page=await browser.new_page();await page.goto(server.base);await page.click('#unlock')
            import struct,math
            raw=b''.join(struct.pack('<h',round(math.sin(n*2*math.pi*440/24000)*800)) for n in range(2400))
            # No task is created. Audio adapter still requires real onended.
            ended=await page.evaluate('(x)=>window.tv.answer(x)',base64.b64encode(raw).decode())
            stopped=await page.evaluate('()=>window.tv.shutdown()')
            return dict(audio_onended=ended,cleanup=stopped,model_calls=0)
        finally:await browser.close()

def seed_service(path,synthesize,f):
    from test_dev_console import FakeRuntime,make_client
    from lira.dictation_service import DictationService
    from lira.textbook_practice import TextbookPractice
    from lira.dictation_task_contract import content_hash
    rt=FakeRuntime();books=path/'books';books.mkdir()
    for key,title in [('tc1-a','合成语文甲'),('tc1-b','合成语文乙')]:
        book=dict(id=key,title=title,subject='chinese',lessons=[dict(unit=1,number=2,title='合成第二课',two=f['two'],four=f['four'],expansions='、'.join(f['words']+f.get('recognition_words',[])),text='仅用于隔离回归的合成课文。')])
        (books/(key+'.json')).write_text(json.dumps(book,ensure_ascii=False),encoding='utf8')
    service=DictationService(rt.store._conn,TextbookPractice(books),path/'cache',synthesize=synthesize,member_exists=lambda n:n==1)
    service.settings.save_v2(dict(phases=['recognition','dictation'],playback=f['playback']),expected_revision=service.settings.get_v2()['revision'])
    # Historical data is fixture setup, predates the one business run. No model,
    # TTS, second preparation call, or fake execution-completion event is used.
    source=dict(book_id='tc1-a',unit=1,number=2,revision=1)
    items=[dict(item_id='prior-'+str(i),lesson_id='prior-lesson',text=w,covers=[c for c in f['four'] if c in w],audio_asset_id='prior-audio') for i,w in enumerate(f['words'])]
    prior=dict(schema_version=2,task_id='historical-fixture',config_revision=1,skill_revision=1,callback_registration_id='historical',lessons=[dict(lesson_id='prior-lesson',source=source,two_targets=f['two'],four_targets=f['four'])],phases=[dict(phase_id='prior-dictation',type='dictation',items=items)],audio_assets=[dict(asset_id='prior-audio',sha256='a'*64,media_type='audio/pcm')],playback=f['playback'])
    prior['content_hash']=content_hash(prior);service.repository.submit(prior,owner_id=1,paired_device_id='historical-device')
    service.results.review(1,prior['task_id'],status='reviewed_with_errors',errors=[dict(item_id=i['item_id'],wrong_characters=[],note='前后鼻音易混；字形难写') for i in items if i['text'] in f['prior_wrong_words']],expected_revision=0,request_id='historical-review')
    rt.conversation_paths=SimpleNamespace(dictation_service=service)
    rt.personal_memory=SimpleNamespace(people=SimpleNamespace(list_members=lambda:[dict(id=1,name='测试孩子')]))
    app,unused,_=make_client(runtime=rt)
    return service,rt,app

async def run_once(*,client,model,synthesize,run_id,artifact_dir,request_options=None,channel='msedge'):
    """Run exactly once. Failure returns a failed report, never retries the case.

    Caller must disable SDK retries (AsyncOpenAI(max_retries=0, timeout=90)).
    synthesize must return actual 24 kHz mono signed little-endian PCM bytes.
    Injected client/TTS ownership remains with caller, even on failure.
    """
    f=load_fixture();path=claim_run(artifact_dir,run_id);record=RunRecord(path,run_id);record.save()
    try:
        if getattr(client,'max_retries',0)!=0:raise ValueError('real model SDK retries must be disabled')
        await asyncio.wait_for(_execute(client,model,synthesize,request_options or {},channel,f,record),timeout=f['max_seconds'])
    except asyncio.TimeoutError:record.finish('timeout',reason='15 minute overall limit reached; no retry')
    except BaseException as exc:
        reason=f'{type(exc).__name__}: {exc}'
        for secret in (getattr(client,'api_key',None),os.environ.get('LIRA_TEST_LLM_API_KEY')):
            if secret:reason=reason.replace(secret,'[redacted]')
        record.finish('failed',reason=reason)
        if isinstance(exc,(KeyboardInterrupt,SystemExit,asyncio.CancelledError)):raise
    else:record.finish('completed')
    return record.data

async def _execute(client,model,synthesize,request_options,channel,f,record):
    import httpx
    from PIL import Image
    from playwright.async_api import async_playwright
    from h5_tv.server import TVServer
    from lira.dictation_agent import DictationAgent
    from lira.dictation_tools import DictationTools,DictationDialogueAgent
    from test_dev_console import PASSPHRASE
    from contextlib import asynccontextmanager
    service,rt,app=seed_service(record.path,synthesize,f)
    original_prepare=service.prepare_task
    prepared_once=False
    async def counted_prepare(*args,**kwargs):
        nonlocal prepared_once
        if prepared_once:record.data['counters']['prepare_after_ready']+=1
        result=await original_prepare(*args,**kwargs)
        if result.get('status')=='ready':prepared_once=True
        return result
    service.prepare_task=counted_prepare
    server=TVServer();tools=None;page=None;browser=None;terminal_snapshot=None
    initial_ids={r['task_id'] for r in service.repository.list_tasks(1)}
    audited=AuditedClient(client,record)
    @asynccontextmanager
    async def cleanup_before_browser_exit():
        try:yield
        finally:
            if tools:await tools.close()
            if page and not page.is_closed():
                try:record.data['cleanup']['h5']=await page.evaluate('()=>window.tv.shutdown()')
                except Exception as exc:record.data['cleanup']['h5_error']=type(exc).__name__
            if browser:await browser.close()
    try:
      async with server.serve(),httpx.AsyncClient(timeout=12) as wire, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://parent.test') as parent,async_playwright() as p,cleanup_before_browser_exit():
        browser=await p.chromium.launch(channel=channel,headless=True,args=['--autoplay-policy=no-user-gesture-required'])
        page=await browser.new_page(viewport=f['viewport'])
        page.on('console',lambda msg:None)
        await page.goto(server.base);await page.click('#unlock')
        service.attach_transport(wire,base=server.base,token=server.token,device_id='h5-tv')
        agent=DictationAgent(audited,model,service,owner_id=1,session_id='tc1-'+record.data['run_id'],paired_device_id='h5-tv',callback_registration_id='tc1-callback',request_options=request_options)
        record.data['counters']['initial_requests']+=1
        first=await agent.respond(f['initial_request']);record.step('01_initial_actual_model_request',result=first)
        if first['status']!='response' or not first.get('text'):raise AssertionError('one actual textbook clarification required')
        prepared=await agent.respond(f['clarification_reply']);record.step('02_clarification_and_preparation',result=prepared)
        if prepared['status']!='ready':raise AssertionError('model did not prepare after the single permitted clarification')
        check_preparation_trace(first,prepared)
        task=prepared['task'];tid=task['task_id'];record.data['task_id']=tid
        current_ids={r['task_id'] for r in service.repository.list_tasks(1)}-initial_ids
        record.data['counters']['new_task_ids']=len(current_ids)
        if current_ids!={tid}:raise AssertionError('business task delta must equal one')
        context=service.sessions[agent.trusted['session_id']]['context']
        facts=context['lessons'][0]['facts'];wrong={e['text'] for r in facts['reviews'] for e in r['errors']}
        if context['owner_id']!=1 or not set(f['prior_wrong_words'])<=wrong:raise AssertionError('targeted child prior facts missing')
        if len(task['phases'])!=2 or task['playback']!=f['playback']:raise AssertionError('pinned phase/config mismatch')
        if not any(t['tool']=='prepare_dictation_task' for t in prepared['trace']):raise AssertionError('missing actual model preparation tool trace')
        record.data['versions']=dict(skill_revision=task['skill_revision'],config_revision=task['config_revision'],content_hash=task['content_hash'],skill_sha256=service.skills.get(task['skill_revision'])['sha256'])
        tools=DictationTools(service,lambda:1,lambda:'tc1-execution',device_id='h5-tv',callback_id='tc1-callback')
        waiting=await tools.call('deliver_dictation_task',dict(task_id=tid))
        if waiting['delivery_state']!='waiting_tv' or server.task:raise AssertionError('offline delivery did not wait')
        record.step('03_offline_waiting',delivery=waiting)
        server.online=True

        async def sync():
            if server.task:await service.transport.poll(tid)
        async def state():return await page.evaluate('()=>window.tv.state()')
        async def until(predicate,label,seconds=50):
            deadline=asyncio.get_running_loop().time()+seconds
            while asyncio.get_running_loop().time()<deadline:
                if not server.disconnected:await sync()
                value=await state()
                if value and predicate(value):
                    if not server.disconnected:
                        await sync()
                        if service.repository.status(tid)['last_sequence']<value['seq']:
                            await asyncio.sleep(.025);continue
                    return value
                await asyncio.sleep(.075)
            raise AssertionError('state deadline: '+label)
        async def child(action):
            if action=='confirm':await until(lambda s:s['state']=='waiting_child' and s['canConfirm'],'confirm debounce ready')
            record.child_turn(action);await page.locator('[data-action="'+action+'"]').click()
        async def screenshot(name):await page.screenshot(path=str(record.path/(name+'.png')),full_page=True)
        await until(lambda s:s['state']=='waiting_start','online receipt')
        await service.transport.lease(tid,6000)
        await service.deliver_task(tid,owner_id=1)
        if sum(e['action']=='task_received' for e in server.events)!=1:raise AssertionError('redelivery restarted task')
        await child('start');await until(lambda s:s['state']=='waiting_child','first recognition page');await screenshot('recognition')
        pages=0
        while (await state())['phase_type']=='recognition':
            before=await state();pages+=1;await child('confirm')
            await until(lambda s:s['seq']>before['seq'] and (s['phase_type']!='recognition' or s['index']>before['index']),'recognition page confirmation')
        if pages<2:raise AssertionError('adaptive recognition did not produce multiple pages')
        record.step('04_recognition_pages',pages=pages)
        words=next(p['items'] for p in task['phases'] if p['type']=='dictation')
        if len(words)<7:raise AssertionError('not enough model-selected covered words for distinct finite faults')

        async def speak(text):
            # Gate checked by production agent before this adapter is entered.
            snapshot=service.repository.status(tid)
            if snapshot['state']!='paused':raise AssertionError('device answer started before actual pause receipt')
            raw=await service._audio(text,1)
            started=time.time();drained=await page.evaluate('(x)=>window.tv.answer(x)',base64.b64encode(raw).decode())
            record.data.setdefault('answer_audio',[]).append(dict(turn=tools.coordinator.turn,started_at=started,drained_at=time.time(),actual_onended=drained,pause_revision=snapshot['state_revision']))
            return drained
        dialogue=DictationDialogueAgent(tools,audited,model,speak=speak,request_options=request_options)
        for index,item in enumerate(words):
            iid=item['item_id']
            await until(lambda s:s['item_id']==iid and (s['state']=='playing' if index==1 else s['state']=='waiting_child'),f'word {index+1} actual audio state')
            before=await state()
            if index!=1 and before['repeats']<3:raise AssertionError('configured repetitions lack real callbacks')
            if index==0:
                await child('replay');await until(lambda s:s['item_id']==iid and s['state']=='waiting_child' and s['repeats']==before['repeats']+1,'one replay')
                await page.evaluate('()=>window.tv.advanceIdle(61000)')
                await until(lambda s:s['reminded'] and s['item_id']==iid and s['state']=='waiting_child','wait reminder')
                record.step('05_replay_and_reminder',state=await state());await screenshot('dictation-safe-status')
            elif index==1:
                record.step('06_question_started_during_playback',state=before)
                for question in ['刚才这个词是哪个词？我没有听清，能解释一下吗？','这个词里面容易写错的字，能再说明一下怎么区分吗？']:
                    turn=record.child_turn(question);result=await dialogue.turn(question,turn=turn)
                    if result['status']!='answered' or not result.get('text'):raise AssertionError('actual model explanation was blocked or incomplete')
                    await sync()
                    if (await state())['state']!='paused' or tools.coordinator.answer_pending:raise AssertionError('answer did not drain while paused')
                    record.step('06_actual_question_round',result=result,state=await state())
                await screenshot('paused-after-actual-explanation')
                # The actual browser child button creates resume_requested under
                # the remote pause lock; production worker consumes it after drain.
                await child('resume');await until(lambda s:s['item_id']==iid and s['state']=='waiting_child' and s['repeats']>=before['repeats']+3,'confirmed assistance resume')
            elif index==2:
                await child('pause');await until(lambda s:s['state']=='paused' and s['item_id']==iid,'ordinary pause')
                await child('resume');await until(lambda s:s['state']=='waiting_child' and s['item_id']==iid,'ordinary resume')
                record.step('07_ordinary_pause',state=await state())
            elif index==3:
                server.disconnected=True
                await until(lambda s:s['state']=='paused' and s['item_id']==iid,'lease expiry',seconds=12)
                server.disconnected=False;await service.transport.lease(tid,6000);await sync();await child('resume')
                await until(lambda s:s['state']=='waiting_child' and s['item_id']==iid,'reconnect child confirmation')
                record.step('08_lease_disconnect',state=await state())
            elif index==4:
                confirmed=before['confirmed'];await page.reload();await page.click('#unlock')
                await until(lambda s:s['state']=='paused' and s['item_id']==iid,'reload checkpoint')
                if (await state())['confirmed']!=confirmed:raise AssertionError('reload lost confirmed cursor')
                await service.transport.lease(tid,6000)
                await child('resume');await until(lambda s:s['state']=='waiting_child' and s['item_id']==iid,'reload child resume')
                record.step('09_reload',state=await state())
            elif index==5:
                # Trigger a genuine asset-fetch failure on a replay attempt. No
                # completion or drain callback is manufactured by the runner.
                await page.evaluate('(id)=>window.tv.failItem(id)',iid);await child('replay')
                await until(lambda s:s['state']=='failed_item' and s['item_id']==iid,'actual asset fetch failure')
                failed=await state()
                if iid in failed['confirmed']:raise AssertionError('failed item counted complete')
                await screenshot('playback-failed')
                await page.evaluate('()=>window.tv.failItem(null)');await child('replay')
                await until(lambda s:s['state']=='waiting_child' and s['item_id']==iid,'explicit playback retry')
                record.step('10_playback_failure_retry',failed=failed,recovered=await state())
            # Safe status/log DOM must never reveal a dictation answer. Actual
            # recognition Han above is allowed; internal immutable data is not DOM.
            visible=await page.locator('body').inner_text()
            if any(w['text'] in visible for w in words):raise AssertionError('dictation answer leaked into TV DOM/log')
            await child('confirm')
        await until(lambda s:s['state']=='completed','single task completion');await sync();await screenshot('completed')
        events=service.repository.events(tid)
        record.data['counters']['task_completed']=sum(e['action']=='task_completed' for e in events)
        if record.data['counters']['task_completed']!=1:raise AssertionError('completion is not unique')
        allids={i['item_id'] for p in task['phases'] for i in p['items']}
        confirmations=[e['item_id'] for e in events if e['action']=='item_confirmed']
        if set(confirmations)!=allids or len(confirmations)!=len(allids):raise AssertionError('confirmation coverage or duplicate counting failure')
        terminal=await state();await page.evaluate('()=>window.tv.action("confirm")')
        if (await state())['seq']!=terminal['seq']:raise AssertionError('late confirm changed terminal task')
        record.step('11_unique_completion',state=terminal)
        # Use actual authenticated parent routes, including same-origin protection.
        unauthorized=await parent.get('/dev/api/dictation')
        if unauthorized.status_code!=401:raise AssertionError('parent auth not enforced')
        login=await parent.post('/dev/login',json=dict(passphrase=PASSPHRASE));login.raise_for_status()
        parent.headers['Origin']='http://parent.test'
        image=io.BytesIO();Image.new('RGB',(320,180),'white').save(image,format='PNG')
        base=f'/dev/api/dictation/member/1/task/{tid}'
        upload=await parent.post(base+'/photo',content=image.getvalue(),headers={'Content-Type':'image/png'});upload.raise_for_status()
        wrong_item=next((i for i in words if i['text']=='清水'),words[0])
        review=await parent.post('/dev/api/dictation/review',json=dict(member_id=1,task_id=tid,status='reviewed_with_errors',errors=[dict(item_id=wrong_item['item_id'],wrong_characters=[wrong_item['text'][0]],note='合成家长核对：前后鼻音与难写字')],expected_revision=0,request_id=run_id_safe(record.data['run_id'])))
        review.raise_for_status()
        knowledge=await parent.post('/dev/api/dictation/facts',json=dict(member_id=1,book_id=f['book_id'],scope_type='lesson',start=2));knowledge.raise_for_status()
        k=knowledge.json()
        if not any(r['task_id']==tid for r in k['lessons'][0]['facts']['reviews']):raise AssertionError('review missing from next scoped knowledge context')
        record.step('12_parent_photo_review_readonly_knowledge',upload=upload.json(),review=review.json(),knowledge=k)
        now_ids={r['task_id'] for r in service.repository.list_tasks(1)}-initial_ids
        if now_ids!={tid}:raise AssertionError('second business task created')
        if record.data['counters']['prepare_after_ready']!=0:raise AssertionError('business prepare called after ready')
        if len([r for r in server.requests if r['operation']=='start'])<1:raise AssertionError('no protocol delivery')
        record.data['business_task_ids']=sorted(now_ids)
        record.data['tv_events']=server.events;record.data['repository_events']=events
        record.data['terminal_state']=await state()
        (record.path/'task.json').write_text(json.dumps(task,ensure_ascii=False,indent=2),encoding='utf8')
        # SQLite evidence is synthetic-only and kept with this unique run.
        import sqlite3
        backup=sqlite3.connect(record.path/'evidence.sqlite3');service.db.backup(backup);backup.close()
    finally:
        if tools:await tools.close()
        if page and not page.is_closed() and 'h5' not in record.data['cleanup']:
            try:record.data['cleanup']['h5']=await page.evaluate('()=>window.tv.shutdown()')
            except Exception as exc:record.data['cleanup']['h5_error']=type(exc).__name__
        if browser:await browser.close()
        record.data['cleanup'].update(browser_closed=True,worker_done=tools is None or tools.worker is None or tools.worker.done(),server_pending=len(server.pending),pending_continue_inputs=0)
        # Preserve failure receipts without exposing Bearer tokens or credentials.
        record.data.setdefault('tv_events',server.events)
        record.save()
        rt.store._conn.close()
    cleanup=record.data['cleanup']
    if cleanup.get('h5')!=dict(timers=0,audioActive=False,pendingInputs=0) or not cleanup.get('worker_done') or cleanup.get('server_pending')!=0:
        raise AssertionError('terminal cleanup incomplete')

def run_id_safe(value):return 'tc1-review-'+value

async def _cli(args):
    if args.preflight:
        print(json.dumps(await preflight(),ensure_ascii=False));return
    from openai import AsyncOpenAI
    if not args.run_id:raise SystemExit('--run-id is required; no automatic IDs or repeat execution')
    if not args.tts_command:raise SystemExit('--tts-command is required: actual PCM JSON-line provider')
    if not os.environ.get('LIRA_TEST_LLM_API_KEY') or not os.environ.get('LIRA_TEST_LLM_MODEL'):raise SystemExit('Test model environment configuration is required')
    # The TTS subprocess receives text/speed JSON lines and returns base64 PCM.
    # No shell=True, credentials, audio fixtures, or household playback.
    import shlex
    proc=await asyncio.create_subprocess_exec(*shlex.split(args.tts_command),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
    async def synthesize(text,speed):
        proc.stdin.write((json.dumps(dict(text=text,speed=speed),ensure_ascii=False)+'\n').encode());await proc.stdin.drain()
        row=json.loads(await asyncio.wait_for(proc.stdout.readline(),90))
        return base64.b64decode(row['pcm'],validate=True)
    client=AsyncOpenAI(api_key=os.environ['LIRA_TEST_LLM_API_KEY'],base_url=os.environ.get('LIRA_TEST_LLM_BASE_URL'),timeout=90,max_retries=0)
    try:
        ready=json.loads(await asyncio.wait_for(proc.stdout.readline(),90))
        if ready.get('ready') is not True:raise RuntimeError('TTS bridge is not ready')
        result=await run_once(client=client,model=os.environ['LIRA_TEST_LLM_MODEL'],synthesize=synthesize,run_id=args.run_id,artifact_dir=args.artifact_dir)
        print(json.dumps(dict(status=result['status'],run_id=result['run_id'],counters=result['counters']),ensure_ascii=False))
        if result['status']!='completed':raise SystemExit(1)
    finally:
        await client.close()
        if proc.returncode is None:
            try:proc.stdin.write(b'{"action":"close"}\n');await proc.stdin.drain();await asyncio.wait_for(proc.wait(),5)
            except Exception:proc.kill();await proc.wait()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id');parser.add_argument('--artifact-dir',default='tmp/dictation-h5-regression');parser.add_argument('--tts-command');parser.add_argument('--preflight',action='store_true')
    asyncio.run(_cli(parser.parse_args()))
