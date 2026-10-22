from types import SimpleNamespace as N
import pytest
from lira.llm.search_agent import run_search_agent

def delta(content=None, reasoning=None, calls=None, finish=None):
    return N(choices=[N(delta=N(content=content, reasoning_content=reasoning, tool_calls=calls), finish_reason=finish)])

def tool(index=0, id=None, name=None, args=None):
    return N(index=index,id=id,type='function',function=N(name=name,arguments=args))

class SDK:
    def __init__(self, rounds):
        self.rounds=iter(rounds); self.requests=[]; self.closed=0; self.searches=[]
        self.chat=N(completions=N(create=self.create))
    async def create(self, **kwargs):
        self.requests.append(kwargs)
        chunks=next(self.rounds)
        sdk=self
        class Stream:
            def __aiter__(self): return self.iterate()
            async def iterate(self):
                for chunk in chunks: yield chunk
            async def close(self): sdk.closed+=1
        return Stream()
    async def post(self,*args,**kwargs):
        self.searches.append(kwargs)
        return {'search_results':[{'title':'来源','url':'https://source.test','snippet':'资料'}]}

async def run(sdk,seen,**kwargs):
    return await run_search_agent(sdk,'kimi-k3',[],lambda:None,voice=True,on_sentence=seen.append,**kwargs)

async def test_no_search_is_one_tool_capable_stream():
    sdk=SDK([[delta('你好。'),delta(finish='stop')]])
    seen=[]
    assert await run(sdk,seen)=='你好。'
    assert seen==['你好。']
    assert len(sdk.requests)==1 and sdk.requests[0]['stream']
    assert sdk.requests[0]['tool_choice']=='auto'
    assert sdk.closed==1

async def test_fragmented_tools_hide_candidate_and_replay_reasoning():
    sdk=SDK([[delta('不应播报',reasoning='隐藏思考',calls=[tool(id='t1',name='search_',args='{"que')]),
              delta(reasoning='后半',calls=[tool(name='web',args='ry":"上海天气"}')]),delta(finish='tool_calls')],
             [delta('有雨。'),delta(finish='stop')]])
    seen=[]
    assert await run(sdk,seen)=='有雨。'
    assert seen==['有雨。'] and len(sdk.searches)==1
    assistant=next(m for m in sdk.requests[1]['messages'] if m.get('tool_calls'))
    assert assistant['reasoning_content']=='隐藏思考后半'
    assert assistant['content']=='不应播报'
    assert assistant['tool_calls'][0]['function']['name']=='search_web'
    assert sdk.closed==2

@pytest.mark.parametrize('chunks',[
    [delta('不完整')],
    [delta('不得播报',calls=[tool(id='t1',name='search_web',args='{}')]),delta(finish='stop')],
    [delta(calls=[tool(id='t1',name='search_web',args='{')]),delta(finish='tool_calls')],
    [delta(reasoning='x'*65537),delta(finish='stop')],
])
async def test_incomplete_or_overlong_protocol_fails_closed(chunks):
    sdk=SDK([chunks]);seen=[]
    reply=await run(sdk,seen)
    assert '完整' in reply and seen==[] and sdk.closed==1 and not sdk.searches

async def test_multiple_tools_are_assembled_by_index_and_never_spoken():
    sdk=SDK([[delta('候选正文',calls=[tool(1,'t2','search_web','{"query":"上海新闻"}'),tool(0,'t1','search_web','{"query":"上海天气"}')]),delta(finish='tool_calls')],
        [delta('查到了。'),delta(finish='stop')]])
    seen=[]
    assert await run(sdk,seen)=='查到了。'
    assert seen==['查到了。'] and len(sdk.searches)==2
    assistant=next(m for m in sdk.requests[1]['messages'] if m.get('tool_calls'))
    assert [c['id'] for c in assistant['tool_calls']]==['t1','t2']

async def test_privacy_change_discards_candidate_and_closes_stream():
    from lira.llm.client import PrivacyBlocked
    sdk=SDK([[delta('不得播报'),delta(finish='stop')]])
    seen=[]
    checks=0
    def check():
        nonlocal checks
        checks+=1
        if checks>=5: raise PrivacyBlocked()
    with pytest.raises(PrivacyBlocked):
        await run_search_agent(sdk,'kimi-k3',[],check,voice=True,on_sentence=seen.append)
    assert not seen and sdk.closed==1 and len(sdk.requests)==1

async def test_cancel_pending_tool_stream_closes_without_retry():
    import asyncio
    entered=asyncio.Event()
    sdk=SDK([])
    class Pending:
        def __aiter__(self): return self
        async def __anext__(self):
            entered.set()
            await asyncio.Event().wait()
        async def close(self): sdk.closed+=1
    async def create(**kwargs):
        sdk.requests.append(kwargs)
        return Pending()
    sdk.chat.completions.create=create
    task=asyncio.create_task(run(sdk,[]))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert sdk.closed==1 and len(sdk.requests)==1

async def test_partial_selection_failure_is_never_retried_or_spoken():
    from openai import APIConnectionError
    import httpx2
    sdk=SDK([]);seen=[]
    class Broken:
        def __aiter__(self): return self.iterate()
        async def iterate(self):
            yield delta('候选正文')
            raise APIConnectionError(request=httpx2.Request('POST','https://unit.test'))
        async def close(self): sdk.closed+=1
    async def create(**kwargs):
        sdk.requests.append(kwargs)
        return Broken()
    sdk.chat.completions.create=create
    with pytest.raises(APIConnectionError): await run(sdk,seen)
    assert not seen and sdk.closed==1 and len(sdk.requests)==1
