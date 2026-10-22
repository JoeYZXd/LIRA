import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import httpx
import pytest

from lira.appliances.store import ApplianceStore
from lira.usage import UsageLedger, AccountBalance


def test_daily_usage_survives_restart_and_splits_at_beijing_midnight(tmp_path):
    clock=[datetime(2026,10,16,23,59,59,tzinfo=timezone(timedelta(hours=8)))]
    path=tmp_path/'usage.db'
    store=ApplianceStore(path)
    ledger=UsageLedger(store,now=lambda:clock[0])
    ledger.add('normal','input_seconds',60)
    ledger.add('normal','sessions')
    clock[0]+=timedelta(seconds=2)
    ledger.add('test','turns',2)
    ledger.add('test','output_seconds',30)
    ledger.flush();store.close()
    store=ApplianceStore(path)
    rows=UsageLedger(store,now=lambda:clock[0]).snapshot()['days']
    assert len(rows)==7
    assert rows[-2]['normal']['input_seconds']==60
    assert rows[-1]['test']['turns']==2
    assert rows[-1]['total']['output_seconds']==30
    assert rows[-1]['normal']['sessions']==0
    store.close()


@pytest.mark.asyncio
async def test_balance_is_cached_and_never_exposes_credentials(monkeypatch):
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.url.params['Action']=='QueryBalanceAcct'
        assert request.headers['Authorization'].startswith('HMAC-SHA256 Credential=test-ak/')
        return httpx.Response(200,json={'Result':{
            'AvailableBalance':'12.50','CashBalance':'20.00','ArrearsBalance':'0'}})
    balance=AccountBalance(env={'VOLCENGINE_ACCESS_KEY_ID':'test-ak',
        'VOLCENGINE_SECRET_ACCESS_KEY':'test-sk'},transport=httpx.MockTransport(handle))
    value=await balance.snapshot()
    assert value['amounts']['AvailableBalance']=='12.50' and value['low_balance']
    assert 'test-ak' not in json.dumps(value) and 'test-sk' not in json.dumps(value)
    await balance.snapshot(refresh=True)
    assert len(calls)==1
    balance._last_attempt-=301
    balance.transport=httpx.MockTransport(lambda request:httpx.Response(403,json={
        'ResponseMetadata':{'Error':{'Message':'secret-should-not-appear'}}}))
    failed=await balance.snapshot()
    assert failed['stale'] and failed['amounts']==value['amounts']
    assert 'secret-should-not-appear' not in str(failed)


@pytest.mark.asyncio
async def test_bad_access_key_is_not_misreported_as_zero_balance():
    balance=AccountBalance(env={'VOLCENGINE_ACCESS_KEY_ID':'ak','VOLCENGINE_SECRET_ACCESS_KEY':'sk'},
        transport=httpx.MockTransport(lambda request:httpx.Response(401,json={
            'ResponseMetadata':{'Error':{'Code':'InvalidAccessKey','Message':'private'}}})))
    result=await balance.snapshot()
    assert result['amounts'] is None and '密钥无效' in result['error']


def test_costs_api_requires_login_and_returns_only_aggregates():
    from test_dev_console import make_client,login
    app,client,rt=make_client()
    rt.conversation_paths=SimpleNamespace(usage=UsageLedger(rt.store))
    rt.conversation_paths.usage.add('test','input_seconds',12)
    rt.dev_handle.account_balance=AccountBalance(env={})
    assert client.get('/dev/api/costs').status_code==401
    login(client)
    response=client.get('/dev/api/costs')
    assert response.headers['cache-control']=='no-store'
    assert response.json()['usage']['days'][-1]['test']['input_seconds']==12
    assert not response.json()['balance']['configured']
