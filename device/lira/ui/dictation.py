"""Authenticated parent views over the shared dictation business service."""
import asyncio
import hashlib
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from lira.dictation_agent import DictationAgent
from lira.dictation_tools import DictationTools


def mount_dictation(app, handle, require_session, privacy_reject):
    templates=Jinja2Templates(directory=str(Path(__file__).parent/'templates'))
    lock=asyncio.Lock(); sessions={}; workers={}

    async def guard(request):
        denied=require_session(request,api=True)
        if denied is not None:return denied
        denied=await privacy_reject()
        if denied is not None:return denied
        if request.method!='GET' and request.headers.get('origin'):
            origin=urlsplit(request.headers['origin']);target=urlsplit(str(request.url))
            if (origin.scheme,origin.netloc)!=(target.scheme,target.netloc):return JSONResponse({'error':'请从本机教学管理操作'},status_code=403)

    def service():
        s=getattr(getattr(handle.runtime,'conversation_paths',None),'dictation_service',None)
        if s is None:raise ValueError('默写服务未配置')
        return s

    def owner(value):
        if type(value) is not int or value<1 or not service().member_exists(value):raise ValueError('请选择有效孩子')
        return value

    async def body(request,limit=512*1024):
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>limit:raise ValueError('内容过大')
            raw.extend(chunk)
        return bytes(raw)

    def failure(exc):
        conflict='conflict' in str(exc).lower()
        return JSONResponse({'error':'版本已更新，编辑已保留，请重新读取后合并。' if conflict else str(exc)},status_code=409 if conflict else 400)

    def session_key(request):
        return hashlib.sha256(request.cookies.get('lira_dev_session','').encode()).hexdigest()

    def session_for(request,sid,mid):
        row=sessions.get(sid)
        if not row or row['parent']!=session_key(request) or row['owner']!=mid:raise ValueError('准备会话已失效，请重新开始')
        return row

    def task_detail(s,mid,tid):
        s.results._task(mid,tid)
        prepared=s.db.execute('SELECT payload FROM dictation_preparation_context WHERE task_id=?',(tid,)).fetchone()
        import json
        provenance=json.loads(prepared[0]).get('provenance',[]) if prepared else []
        return dict(task=s.repository.snapshot(tid),state=s.repository.status(tid),events=s.repository.events(tid),review=s.results.get(mid,tid),photos=s.results.photos(mid,tid),provenance=provenance,tts=json.loads(prepared[0]).get('context',{}).get('config',{}).get('parameters',{}).get('tts') if prepared else None)

    def tools_for(s,mid,tid):
        existing=getattr(s,'_task_workers',{}).get(tid)
        if existing and (existing.coordinator.question or existing.coordinator.speaking or existing.coordinator.answer_pending):return existing
        voice=getattr(getattr(handle.runtime,'conversation_paths',None),'dictation',None)
        if voice and voice.owner()==mid:
            old=workers.pop(tid,None)
            if old:
                old.coordinator.close()
                if old.worker:old.worker.cancel()
            voice.adopt(tid)
            return voice
        if tid not in workers:
            t=DictationTools(s,lambda:mid,lambda:'parent:'+tid)
            t.adopt(tid);workers[tid]=t
        return workers[tid]

    @app.get('/dev/dictation')
    async def page(request:Request):
        denied=require_session(request,api=False)
        if denied is not None:return denied
        return templates.TemplateResponse(request,'dictation.html',{})

    @app.get('/dev/api/dictation')
    async def index(request:Request):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            s=service();people=getattr(getattr(handle.runtime,'personal_memory',None),'people',None)
            return dict(members=people.list_members() if people else [],books=s.context.textbooks.catalog(),skill=s.skills.get(),config=s.settings.get_v2(),display=s.settings.get_display())
        except ValueError as exc:return failure(exc)

    @app.get('/dev/api/dictation/member/{mid}')
    async def member(request:Request,mid:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            s=service();mid=owner(int(mid))
            return dict(profile=s.profiles.get(mid),tasks=s.repository.list_tasks(mid))
        except ValueError as exc:return failure(exc)

    @app.get('/dev/api/dictation/member/{mid}/task/{tid}')
    async def detail(request:Request,mid:str,tid:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:return task_detail(service(),owner(int(mid)),tid)
        except ValueError as exc:return failure(exc)

    @app.get('/dev/api/dictation/member/{mid}/photo/{aid}')
    async def photo(request:Request,mid:str,aid:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            result=service().results.read_photo(owner(int(mid)),aid)
            return Response(result['data'],media_type=result['content_type'],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except ValueError as exc:return failure(exc)

    @app.post('/dev/api/dictation/member/{mid}/task/{tid}/photo')
    async def upload(request:Request,mid:str,tid:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            data=await body(request,10*1024*1024)
            async with lock:
                denied=await guard(request)
                if denied is not None:return denied
                row=service().results.upload(owner(int(mid)),tid,data,content_type=request.headers.get('content-type'),replace_id=request.query_params.get('replace_id'))
            return dict(photo=row)
        except (ValueError,TypeError) as exc:return failure(exc)

    @app.post('/dev/api/dictation/{action}')
    async def mutate(request:Request,action:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            import json
            data=json.loads(await body(request))
            if type(data) is not dict:raise ValueError('请求格式无效')
            async with lock:
                denied=await guard(request)
                if denied is not None:return denied
                s=service()
                if action=='display':
                    settings=s.settings.save_display(data.get('debug_enabled'),expected_revision=data.get('expected_revision'))
                    return dict(display=settings,television=await s.sync_display())
                if action=='skill':return dict(skill=s.skills.save(data.get('content'),expected_revision=data.get('expected_revision')))
                if action=='config':return dict(config=s.settings.save_v2(data.get('parameters'),expected_revision=data.get('expected_revision')))
                mid=owner(data.get('member_id'))
                if action=='profile':return dict(profile=s.profiles.save(mid,data.get('book_id'),data.get('progress',{}),expected_revision=data.get('expected_revision')))
                if action=='facts':return s.context.resolve(data,mid)
                if action in ('prepare','confirm_candidates'):
                    sid=data.get('session_id')
                    if sid:row=session_for(request,sid,mid)
                    else:
                        if len(sessions)>=64:raise ValueError('准备会话数量已达上限，请重启页面服务后再试')
                        rt=handle.runtime;client=getattr(getattr(rt,'_llm',None),'_client',None)
                        model=getattr(getattr(getattr(rt,'config',None),'llm',None),'model',None) or getattr(getattr(rt,'_llm',None),'_model',None)
                        if not client or not model:raise ValueError('大模型未配置，无法准备任务')
                        sid=uuid.uuid4().hex;voice=getattr(rt.conversation_paths,'dictation',None)
                        s.begin_session(mid,sid,turn='parent-new-task:'+uuid.uuid4().hex)
                        options=getattr(rt._llm,'_voice_options',lambda _:{})(True)
                        agent=DictationAgent(client,model,s,owner_id=mid,session_id=sid,paired_device_id=getattr(voice,'device_id','unpaired'),callback_registration_id=getattr(voice,'callback_id','local'),request_options=options)
                        row=dict(parent=session_key(request),owner=mid,agent=agent);sessions[sid]=row
                    if action=='confirm_candidates':
                        s.confirm_candidates(mid,sid,data.get('confirmation_id'),turn='parent-click:'+uuid.uuid4().hex)
                        text='已确认采用刚才候选词，请继续准备。'
                    else:text=data.get('text')
                    if type(text) is not str or not 0<len(text)<=4000:raise ValueError('请输入任务或回复')
                    if len(row['agent'].messages)>180:raise ValueError('准备会话过长，请新建会话')
                    result=await row['agent'].respond(text)
                    denied=await guard(request)
                    if denied is not None:return denied
                    return dict(result,session_id=sid,pending_candidates=s.sessions.get(sid,{}).get('pending_candidates'))
                tid=data.get('task_id');s.results._task(mid,tid)
                if action=='deliver':
                    result=await s.deliver_task(tid,owner_id=mid);tools_for(s,mid,tid).start_worker()
                    return result
                if action=='control':
                    t=tools_for(s,mid,tid);a=data.get('action')
                    phrase={'start':'开始','resume':'继续','confirm':'写好了'}.get(a)
                    if phrase:t.coordinator.observe_user(phrase,turn=t.coordinator.turn+1)
                    return await t.coordinator.control(a)
                if action=='review':return dict(review=s.results.review(mid,tid,status=data.get('status'),errors=data.get('errors',[]),recognition_errors=data.get('recognition_errors',[]),note=data.get('note',''),expected_revision=data.get('expected_revision'),request_id=data.get('request_id')))
                if action=='delete_photo':
                    s.results.delete_photo(mid,data.get('attachment_id'));return dict(status='deleted')
                raise ValueError('未知操作')
        except (ValueError,TypeError,KeyError) as exc:return failure(exc)

    @app.on_event('shutdown')
    async def stop_workers():
        for t in list(workers.values()):await t.close()
