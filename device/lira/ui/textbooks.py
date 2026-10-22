"""Authenticated textbook drafts; publication is a separate explicit action."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from lira.ui.static_assets import asset_versions


def mount_textbooks(app,handle,require_session,privacy_reject):
    templates=Jinja2Templates(directory=str(Path(__file__).parent/'templates'))
    store=None
    tasks={}
    mutation=asyncio.Lock()

    def library():
        nonlocal store
        if store is None:
            from lira.textbook_import import TextbookImportStore
            configured=os.environ.get('LIRA_TEXTBOOK_DIR')
            if not configured:raise ValueError('教材存储尚未配置')
            books=Path(configured)
            root=Path(os.environ.get('LIRA_TEXTBOOK_IMPORT_DIR',str(books.parent/'textbook-imports')))
            store=TextbookImportStore(root,books)
        return store

    async def guard(request):
        denied=require_session(request,api=True)
        if denied is not None:return denied
        denied=await privacy_reject()
        if denied is not None:return denied
        if request.method!='GET' and request.headers.get('origin'):
            origin=urlsplit(request.headers['origin']);target=urlsplit(str(request.url))
            if (origin.scheme,origin.netloc)!=(target.scheme,target.netloc):
                return JSONResponse({'error':'请从本机教材页面操作'},status_code=403)
        return None

    async def body(request):
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>1024*1024:raise ValueError('编辑内容过大')
            raw.extend(chunk)
        import json
        result=json.loads(raw)
        if not isinstance(result,dict):raise ValueError('请求格式无效')
        return result

    def error(exc):
        if isinstance(exc,FileNotFoundError):return JSONResponse({'error':'未找到草稿或页面'},status_code=404)
        message=str(exc) if isinstance(exc,ValueError) else '教材操作失败，请稍后重试'
        code=409 if '草稿已更新' in message or '当前草稿不可' in message else 400
        return JSONResponse({'error':message},status_code=code)

    previous=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        try:
            async with previous(application) as state:yield state
        finally:
            for task in tasks.values():task.cancel()
            await asyncio.gather(*tasks.values(),return_exceptions=True)
    app.router.lifespan_context=lifespan

    @app.get('/dev/textbooks')
    async def page(request:Request):
        denied=require_session(request,api=False)
        if denied is not None:return denied
        return templates.TemplateResponse(request,'textbooks.html',{
            'asset_versions':asset_versions('textbooks.js','textbooks.css','teaching-embed.js')
        },headers={'Cache-Control':'no-store'})

    @app.get('/dev/api/textbooks')
    async def index(request:Request):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            from lira.textbook_practice import TextbookPractice
            books=await asyncio.to_thread(TextbookPractice(library().books_dir).catalog, include_drafts=False)
            drafts=await asyncio.to_thread(library().list_drafts)
            return JSONResponse(dict(books,drafts=drafts),headers={'Cache-Control':'no-store'})
        except (ValueError,OSError) as exc:return error(exc)

    @app.post('/dev/api/textbooks/import')
    async def upload(request:Request):
        denied=await guard(request)
        if denied is not None:return denied
        if mutation.locked():return JSONResponse({'error':'已有文件正在导入，请稍后重试'},status_code=409)
        try:
            limit=100*1024*1024
            length=int(request.headers.get('content-length','0'))
            if not 0<length<=limit+1024*1024:raise ValueError('上传总大小限 100 MB，且必须包含文件长度')
            async with mutation:
                async with request.form(max_files=200,max_fields=4) as form:
                    files=[];size=0
                    for uploaded in form.getlist('files'):
                        if not hasattr(uploaded,'read'):raise ValueError('请选择课本文件')
                        raw=await uploaded.read(limit-size+1);size+=len(raw)
                        if size>limit:raise ValueError('上传总大小不能超过 100 MB')
                        files.append((uploaded.filename or '课本',raw))
                    draft=await asyncio.to_thread(library().create,files)
                return JSONResponse({'draft':draft},status_code=201)
        except (ValueError,OSError) as exc:return error(exc)

    @app.get('/dev/api/textbooks/books/{book_id}')
    async def book(request:Request,book_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:return {'book':await asyncio.to_thread(library().read_book,book_id)}
        except (ValueError,OSError) as exc:return error(exc)

    @app.post('/dev/api/textbooks/books/{book_id}/revise')
    async def revise_book(request:Request,book_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            async with mutation:
                denied=await privacy_reject()
                if denied is not None:return denied
                return {'draft':await asyncio.to_thread(library().revise_book,book_id)}
        except (ValueError,OSError) as exc:return error(exc)

    @app.put('/dev/api/textbooks/books/{book_id}/cover')
    async def book_cover(request:Request,book_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            data=await body(request)
            async with mutation:
                denied=await privacy_reject()
                if denied is not None:return denied
                return {'book':await asyncio.to_thread(library().set_book_cover,
                    book_id,data.get('revision'),data.get('cover_page'))}
        except (ValueError,OSError) as exc:return error(exc)

    @app.api_route('/dev/api/textbooks/drafts/{draft_id}',methods=['GET','PUT'])
    async def draft(request:Request,draft_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            if request.method=='PUT':result=await asyncio.to_thread(library().update,draft_id,await body(request))
            else:
                result=await asyncio.to_thread(library().get,draft_id)
                if result['status']=='recognizing' and (draft_id not in tasks or tasks[draft_id].done()):
                    result=await asyncio.to_thread(library().fail_recognition,draft_id,'上次识别已中断，保留原页和文字，可重新识别。')
            return {'draft':result}
        except (ValueError,OSError) as exc:return error(exc)

    @app.get('/dev/api/textbooks/drafts/{draft_id}/pages/{number}')
    async def source(request:Request,draft_id:str,number:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            path=await asyncio.to_thread(library().page_path,draft_id,int(number))
            return FileResponse(path,media_type='image/png',headers={'Cache-Control':'no-store'})
        except (ValueError,OSError) as exc:return error(exc)

    @app.get('/dev/api/textbooks/drafts/{draft_id}/pages/{number}/thumbnail')
    async def thumbnail(request:Request,draft_id:str,number:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            raw=await asyncio.to_thread(library().page_thumbnail,draft_id,int(number))
            return Response(raw,media_type='image/jpeg',headers={'Cache-Control':'no-store'})
        except (ValueError,OSError) as exc:return error(exc)

    @app.post('/dev/api/textbooks/drafts/{draft_id}/recognize')
    async def recognize(request:Request,draft_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        if any(not task.done() for task in tasks.values()):
            return JSONResponse({'error':'已有课本正在识别，请稍后重试'},status_code=409)
        try:
            from lira.textbook_recognizer import recognize_textbook
            result=library().set_recognizing(draft_id)
            tasks.clear()
            tasks[draft_id]=asyncio.create_task(recognize_textbook(library(),draft_id,handle.runtime,handle.dev_mutex))
            return JSONResponse({'draft':result},status_code=202)
        except (ValueError,OSError) as exc:return error(exc)

    @app.post('/dev/api/textbooks/drafts/{draft_id}/publish')
    async def publish(request:Request,draft_id:str):
        denied=await guard(request)
        if denied is not None:return denied
        try:
            data=await body(request)
            result=await asyncio.to_thread(library().publish,draft_id,data.get('revision'),data.get('confirmed'))
            return {'book':result}
        except (ValueError,OSError) as exc:return error(exc)
