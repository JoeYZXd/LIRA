import asyncio
import json

import httpx
import pytest

from lira.tv_display import TvDisplaySender


def sender(tmp_path,monkeypatch,handler):
    config=tmp_path/'pair.json';config.write_text(json.dumps({'host':'192.168.3.25','token':'x'*64}))
    real=httpx.AsyncClient
    monkeypatch.setattr('lira.tv_display.httpx.AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler),**kw))
    return TvDisplaySender(config)


@pytest.mark.asyncio
@pytest.mark.parametrize('initial',[None,{'interactive':True,'foreground':False}])
async def test_explicit_readiness_launches_once_then_checks_real_receiver(tmp_path,monkeypatch,initial):
    launched=False;calls=[]
    def handler(req):
        nonlocal launched
        calls.append(req.url.port)
        if req.url.port==6095:
            assert 'authorization' not in req.headers
            assert req.url.params['packagename']=='org.lira.receiver'
            launched=True
            return httpx.Response(200,json={'status':0})
        if launched:return httpx.Response(200,json={'interactive':True,'foreground':True})
        if initial is None:raise httpx.ConnectError('stopped',request=req)
        return httpx.Response(200,json=initial)
    result=await sender(tmp_path,monkeypatch,handler).ensure_ready()
    assert result['interactive'] and result['foreground'] and calls.count(6095)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('state',[{'interactive':False,'foreground':False},{'interactive':True,'foreground':True}])
async def test_readiness_off_or_already_ready_never_launches(tmp_path,monkeypatch,state):
    calls=[]
    def handler(req):calls.append(req.url.port);return httpx.Response(200,json=state)
    assert await sender(tmp_path,monkeypatch,handler).ensure_ready()=={key:state.get(key) for key in ('version','capabilities','interactive','foreground','rendered_id','instance','audio')}
    assert calls==[40100]


@pytest.mark.asyncio
async def test_readiness_auth_error_never_launches_and_probe_stays_read_only(tmp_path,monkeypatch):
    calls=[]
    def handler(req):calls.append(req.url.port);return httpx.Response(401)
    target=sender(tmp_path,monkeypatch,handler)
    with pytest.raises(httpx.HTTPStatusError):await target.ensure_ready()
    with pytest.raises(httpx.HTTPStatusError):await target.probe()
    assert calls==[40100,40100]


@pytest.mark.asyncio
async def test_explicit_readiness_cancel_stops_polling(tmp_path,monkeypatch):
    entered=asyncio.Event();calls=[]
    async def handler(req):
        calls.append(req.url.port)
        if req.url.port==40100:raise httpx.ConnectError('stopped',request=req)
        entered.set();await asyncio.Event().wait()
    task=asyncio.create_task(sender(tmp_path,monkeypatch,handler).ensure_ready())
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert calls==[40100,6095]


@pytest.mark.asyncio
async def test_readiness_failed_launch_is_not_reported_as_ready(tmp_path,monkeypatch):
    calls=[]
    def handler(req):
        calls.append(req.url.port)
        if req.url.port==40100:raise httpx.ConnectError('stopped',request=req)
        return httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):await sender(tmp_path,monkeypatch,handler).ensure_ready()
    assert calls==[40100,6095]


@pytest.mark.asyncio
async def test_launch_no_credential_leak_then_rendered_ack(tmp_path,monkeypatch):
    calls=[];ready=False
    def handler(req):
        nonlocal ready
        calls.append((req.method,req.url.path))
        if req.url.port==6095:
            assert 'authorization' not in req.headers
            assert req.url.params['action']=='startapp'
            ready=True
            raise httpx.ReadTimeout('legacy endpoint',request=req)
        assert req.headers['authorization']=='Bearer '+'x'*64
        if not ready:raise httpx.ConnectError('offline',request=req)
        return httpx.Response(200,json={'interactive':True,'foreground':True,'rendered_id':'one'})
    result=await sender(tmp_path,monkeypatch,handler).show({'id':'one','approved':True,'type':'text','text':'hello'})
    assert result['rendered_id']=='one'
    assert ('POST','/show') in calls


@pytest.mark.asyncio
async def test_screen_off_never_launches_or_posts(tmp_path,monkeypatch):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200,json={'interactive':False})
    with pytest.raises(RuntimeError):await sender(tmp_path,monkeypatch,handler).show({'id':'one'})
    assert calls==['/status']


@pytest.mark.asyncio
async def test_auth_failure_does_not_launch(tmp_path,monkeypatch):
    calls=[]
    def handler(req):calls.append(req.url.path);return httpx.Response(401)
    with pytest.raises(httpx.HTTPStatusError):await sender(tmp_path,monkeypatch,handler).show({'id':'one'})
    assert calls==['/status']


@pytest.mark.asyncio
async def test_cancel_before_ready_prevents_post(tmp_path,monkeypatch):
    started=asyncio.Event();calls=[]
    async def handler(req):
        calls.append(req.url.path);started.set();await asyncio.sleep(60)
    task=asyncio.create_task(sender(tmp_path,monkeypatch,handler).show({'id':'one'}))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert '/show' not in calls


@pytest.mark.asyncio
async def test_clear_requires_new_receiver_and_never_launches(tmp_path,monkeypatch):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200,json={'interactive':True,'foreground':True,'version':'0.1'})
    with pytest.raises(RuntimeError):await sender(tmp_path,monkeypatch,handler).clear('one')
    assert calls==['/status']


@pytest.mark.asyncio
async def test_clear_ack_is_bound_to_expected_id(tmp_path,monkeypatch):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        if req.url.path=='/status':return httpx.Response(200,json={'interactive':True,'foreground':True,'capabilities':['clear_content_id']})
        assert json.loads(req.content)=={'expected_content_id':'one'}
        return httpx.Response(200,json={'cleared_id':'one'})
    assert (await sender(tmp_path,monkeypatch,handler).clear('one'))['cleared_id']=='one'
    assert calls==['/status','/clear']


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[{'interactive':False,'foreground':True},{'interactive':True,'foreground':False}])
async def test_clear_does_not_post_when_screen_off_or_dismissed(tmp_path,monkeypatch,status):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200,json=dict(status,capabilities=['clear_content_id']))
    with pytest.raises(RuntimeError):await sender(tmp_path,monkeypatch,handler).clear('one')
    assert calls==['/status']


@pytest.mark.asyncio
@pytest.mark.parametrize('reply',[{'cleared_id':'other'},{'accepted_id':'one'},{}])
async def test_clear_never_accepts_unmatched_ack(tmp_path,monkeypatch,reply):
    def handler(req):
        return httpx.Response(200,json=({'interactive':True,'foreground':True,'capabilities':['clear_content_id']}
            if req.url.path=='/status' else reply))
    with pytest.raises(RuntimeError):await sender(tmp_path,monkeypatch,handler).clear('one')


@pytest.mark.asyncio
async def test_clear_content_replaced_conflict_does_not_fallback_show(tmp_path,monkeypatch):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        if req.url.path=='/status':return httpx.Response(200,json={'interactive':True,'foreground':True,'capabilities':['clear_content_id']})
        return httpx.Response(409,json={'error':'content mismatch'})
    with pytest.raises(httpx.HTTPStatusError):await sender(tmp_path,monkeypatch,handler).clear('one')
    assert calls==['/status','/clear']


@pytest.mark.asyncio
async def test_clear_offline_never_launches(tmp_path,monkeypatch):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        raise httpx.ConnectError('offline',request=req)
    with pytest.raises(httpx.ConnectError):await sender(tmp_path,monkeypatch,handler).clear('one')
    assert calls==['/status']
