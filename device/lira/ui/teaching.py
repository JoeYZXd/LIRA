"""Parent management only; these endpoints are never model tools."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates


def mount_teaching(app, handle, require_session, privacy_reject):
    from .dictation import mount_dictation
    mount_dictation(app,handle,require_session,privacy_reject)
    templates=Jinja2Templates(directory=str(Path(__file__).parent/'templates'))
    mutation=asyncio.Lock()

    async def guard(request):
        denied=require_session(request,api=True)
        if denied is not None:return denied
        denied=await privacy_reject()
        if denied is not None:return denied
        if request.method!='GET' and request.headers.get('origin'):
            origin=urlsplit(request.headers['origin']);target=urlsplit(str(request.url))
            if (origin.scheme,origin.netloc)!=(target.scheme,target.netloc):
                return JSONResponse({'error':'请从本机教学管理页面操作'},status_code=403)

    def paths():
        value=getattr(handle.runtime,'conversation_paths',None)
        if value is None:raise ValueError('教学服务未配置')
        return value

    async def body(request):
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>512*1024:raise ValueError('内容过大')
            raw.extend(chunk)
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('请求格式无效')
        return value

    def integer(value):
        if type(value) is not int or value<0:raise ValueError('编号或版本无效')
        return value

    def error(exc):
        message=str(exc)
        if isinstance(exc,PermissionError):return JSONResponse({'error':'隐私模式开启，操作已取消'},status_code=403)
        conflict='conflict' in message.lower()
        return JSONResponse({'error':'版本已被其他操作更新，请保留编辑并重新读取。' if conflict else message},status_code=409 if conflict else 400)

    @app.get('/dev/teaching')
    async def page(request:Request):
        denied=require_session(request,api=False)
        if denied is not None:return denied
        return templates.TemplateResponse(request,'teaching.html',{})

    @app.get('/dev/api/teaching')
    async def index(request:Request):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            p=paths();session=p.session
            available=bool(p.capabilities_enabled and session is not None
                and not getattr(session,'closed',False) and not getattr(session,'_closed',False)
                and not getattr(session,'failed',False))
            executor=getattr(session,'capabilities',None) if available else None
            if executor is None or executor.closed:available=False;executor=None
            people=getattr(getattr(handle.runtime,'personal_memory',None),'people',None)
            return {'model_available':available,'capabilities':[
                dict(row, status=row['status'] if available else 'unavailable') for row in p.capabilities.status()],
                'task':p.agent_task.snapshot(),'operations':[executor._snapshot(op) for op in executor.operations.values()] if executor else [],
                'contents':p.teaching_content.list(),'members':people.list_members() if people else []}
        except ValueError as exc:return error(exc)

    @app.post('/dev/api/teaching/content')
    async def create(request:Request):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            data=await body(request)
            async with mutation:
                denied=await privacy_reject()
                if denied is not None:return denied
                row=paths().teaching_content.save_draft(title=data.get('title'),text=data.get('text'),kind=data.get('kind'),source=data.get('source'))
            return {'content':row}
        except (ValueError,TypeError,PermissionError) as exc:return error(exc)

    @app.post('/dev/api/teaching/import')
    async def import_pages(request:Request):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            import os
            from lira.textbook_import import TextbookImportStore
            data=await body(request)
            books=os.environ.get('LIRA_TEXTBOOK_DIR')
            if not books:raise ValueError('教材存储尚未配置')
            store=TextbookImportStore(Path(os.environ.get('LIRA_TEXTBOOK_IMPORT_DIR',str(Path(books).parent/'textbook-imports'))),Path(books))
            async with mutation:
                denied=await privacy_reject()
                if denied is not None:return denied
                row=paths().teaching_content.import_pages(store,data.get('draft_id'),title=data.get('title'),kind=data.get('kind'),page_numbers=data.get('page_numbers'),expected_revision=integer(data.get('expected_revision')))
            return {'content':row}
        except (ValueError,TypeError,PermissionError,FileNotFoundError) as exc:return error(exc)

    @app.api_route('/dev/api/teaching/content/{content_id}',methods=['GET','PUT','DELETE'])
    async def content(request:Request,content_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            p=paths()
            if request.method=='GET':
                row=p.teaching_content.get_draft(content_id)
                if row['status']=='not_found':return JSONResponse({'error':'原文已删除或不存在'},status_code=404)
                return {'content':row}
            data=await body(request);integer(data.get('expected_version'))
            async with mutation:
                denied=await privacy_reject()
                if denied is not None:return denied
                result=await p.manage_teaching_content(content_id,data,delete=request.method=='DELETE')
            return {'content':result}
        except (ValueError,TypeError,PermissionError) as exc:return error(exc)

    @app.post('/dev/api/teaching/content/{content_id}/confirm')
    async def confirm(request:Request,content_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            data=await body(request);version=integer(data.get('version'))
            async with mutation:
                denied=await privacy_reject()
                if denied is not None:return denied
                result=paths().teaching_content.confirm(content_id,version)
            return {'content':result}
        except (ValueError,TypeError,PermissionError) as exc:return error(exc)

    @app.api_route('/dev/api/teaching/records',methods=['POST'])
    async def records(request:Request):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            data=await body(request);mid=integer(data.get('member_id'));p=paths()
            async with mutation:
                denied=await privacy_reject()
                if denied is not None:return denied
                action=data.get('action','list')
                if action=='list':return {'records':p.teaching_memory.parent_records(mid)}
                revision=integer(data.get('expected_revision'))
                rid=data.get('record_id')
                if rid is not None and (not isinstance(rid,str) or not 1<=len(rid)<=64):
                    raise ValueError('记录编号无效')
                if action=='delete':
                    async with p._lock:
                        row=next((r for r in p.teaching_memory.parent_records(mid) if r['id']==rid),None)
                        if row is None or row['revision']!=revision:raise ValueError('Revision conflict')
                        await p._stop()
                        result=p.teaching_memory.parent_delete(mid,data.get('record_id'),revision)
                else:
                    common=dict(member_id=mid,expected_revision=revision,request_id=data.get('request_id'),result=data.get('result'),note=data.get('note',''))
                    if action=='paper':result=p.teaching_memory.parent_review(record_id=data.get('record_id'),**common)
                    elif action=='annotation':result=p.teaching_memory.parent_annotate_recitation(evidence_id=data.get('record_id'),**common)
                    else:raise ValueError('操作无效')
            return {'record':result}
        except (ValueError,TypeError,PermissionError) as exc:return error(exc)
