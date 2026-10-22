"""Offline functional preflight. Real TC1 requires the explicit runner command.

This test uses synthetic model responses and a synthesized tone, never a cloud
provider. It exercises actual production tool handlers and actual H5 onended.
"""
import asyncio
import importlib.util
import json
import math
from pathlib import Path
import struct
from types import SimpleNamespace
import pytest

DEVICE=Path(__file__).resolve().parents[2]

def runner():
    spec=importlib.util.spec_from_file_location('tc1_preflight',DEVICE/'scripts'/'run_dictation_h5_regression.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

class SyntheticModel:
    """Only test fixture responses. Production service receives every tool call."""
    def __init__(self,fixture):
        self.fixture=fixture;self.serial=0
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
    async def create(self,**kw):
        from openai.types.chat import ChatCompletionMessage
        self.serial+=1;last=kw['messages'][-1]
        def call(name,args):
            return dict(role='assistant',tool_calls=[dict(id='synthetic-'+str(self.serial),type='function',function=dict(name=name,arguments=json.dumps(args,ensure_ascii=False)))])
        if last['role']=='user':
            text=last['content']
            if text==self.fixture['initial_request']:out=dict(role='assistant',content='请问使用合成语文甲还是合成语文乙？')
            elif text==self.fixture['clarification_reply']:out=call('get_dictation_context',dict(book_id='tc1-a',scope_type='lesson',start=2))
            else:out=call('get_dictation_task_status',{})
        elif last['role']=='tool':
            data=json.loads(last['content']);name=kw['messages'][-2]['tool_calls'][0]['function']['name']
            if name=='get_dictation_context':
                if data['status']!='ready':out=dict(role='assistant',content='请问使用合成语文甲还是合成语文乙？')
                else:out=call('prepare_dictation_task',dict(proposals={'2':dict(words=self.fixture['words'],reason='合成测试完整覆盖，清水与声音的历史错词已考虑')}))
            elif name=='get_dictation_task_status':
                if data['state']!='paused':out=call('control_dictation_task',dict(action='pause'))
                else:out=dict(role='assistant',content='这是当前词语的说明，请区分字形和前后鼻音。明白后请确认继续。')
            else:out=dict(role='assistant',content='已经实际暂停。这是当前词语的说明，请区分字形和前后鼻音。明白后请确认继续。')
        else:raise AssertionError('unexpected synthetic conversation')
        message=ChatCompletionMessage(**out)
        return SimpleNamespace(id='offline-synthetic-'+str(self.serial),choices=[SimpleNamespace(message=message)])

@pytest.mark.asyncio
async def test_offline_h5_full_protocol_preflight(tmp_path):
    m=runner();f=m.load_fixture();record=m.RunRecord(tmp_path,'OFFLINE-PREFLIGHT')
    record.data['evidence_type']='OFFLINE synthetic model/tone; NOT real-model TC1 evidence'
    async def synthesize(text,speed):
        return b''.join(struct.pack('<h',round(math.sin(i*2*math.pi*440/24000)*800)) for i in range(960))
    try:
        await asyncio.wait_for(m._execute(SyntheticModel(f),'OFFLINE-synthetic',synthesize,{},'msedge',f,record),180)
    except Exception as exc:
        record.finish('failed',reason=str(exc));raise
    else:record.finish('completed')
    assert record.data['counters']['new_task_ids']==1
    assert record.data['counters']['task_completed']==1
    assert record.data['cleanup']['h5']==dict(timers=0,audioActive=False,pendingInputs=0)
    assert record.data['cleanup']['worker_done']
    assert len(record.data['answer_audio'])==2
    assert all(a['actual_onended'] for a in record.data['answer_audio'])

@pytest.mark.asyncio
async def test_real_model_tc1_explicit_entry_only():
    """Default test discovery must never spend model calls or retry the case."""
    import os
    if os.environ.get('LIRA_RUN_REAL_TC1'):
        pytest.fail('Use the explicit one-shot runner CLI, never pytest retries for real TC1')
    pytest.skip('Real TC1 is an explicit run_once/CLI operation with unique run ID')

@pytest.mark.asyncio
async def test_h5_remote_pause_lock_survives_local_pause_and_reload(tmp_path):
    m=runner();f=m.load_fixture()
    from h5_tv.server import TVServer
    from playwright.async_api import async_playwright
    s,rt,_=m.seed_service(tmp_path,lambda text,speed:b'\x01\x00'*240,f)
    prepared=await s.prepare_task(dict(book_id='tc1-a',scope_type='lesson',start=2,proposals={'2':dict(words=f['words'])}),owner_id=1,session_id='lock',paired_device_id='tv',callback_registration_id='cb')
    task=prepared['task'];server=TVServer();server.task=task;server.epoch=1
    checkpoint=dict(task=task,epoch=1,state='paused',phase=1,index=0,seq=0,revision=0,repeats=0,confirmed=[],outbox=[],remote_pause_lock=True)
    async with server.serve(),async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True)
        try:
            page=await browser.new_page();await page.goto(server.base)
            await page.evaluate('(s)=>localStorage.setItem("tc1-tv",JSON.stringify(s))',checkpoint)
            await page.reload();await page.wait_for_function('()=>window.tv.state()?.seq===1')
            await page.evaluate('()=>window.tv.action("pause")')
            assert (await page.evaluate('()=>window.tv.state()'))['remoteLock']
            await page.reload();await page.wait_for_function('()=>window.tv.state()?.seq>=3')
            assert (await page.evaluate('()=>window.tv.state()'))['remoteLock']
            await server.dispatch('lease',dict(lease_ms=6000))
            await page.evaluate('()=>window.tv.action("resume")')
            state=await page.evaluate('()=>window.tv.state()')
            assert state['state']=='paused' and state['remoteLock']
            assert server.events[-1]['action']=='resume_requested'
            await page.evaluate('()=>window.tv.shutdown()')
        finally:await browser.close();rt.store._conn.close()
