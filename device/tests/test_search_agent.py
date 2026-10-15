from types import SimpleNamespace
import json
import pytest
from lira.llm.search_agent import run_search_agent
from lira.llm.client import PrivacyBlocked


class Message:
    def __init__(self, content=None, calls=None):
        self.content, self.tool_calls = content, calls
    def model_dump(self, **kwargs):
        return {'role': 'assistant', 'content': self.content,
                'tool_calls': [{'id': c.id, 'type': 'function', 'function':
                    {'name': c.function.name, 'arguments': c.function.arguments}}
                    for c in self.tool_calls or []]}


def call(query='上海明天天气', name='search_web'):
    return SimpleNamespace(id='tool1', function=SimpleNamespace(
        name=name, arguments=json.dumps({'query': query})))


class Fake:
    def __init__(self, messages):
        self.messages, self.requests, self.searches = iter(messages), [], []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=next(self.messages))])
    async def post(self, path, **kwargs):
        self.searches.append(kwargs['body'])
        return {'search_results': [{'title':'气象台', 'url':'https://weather.example',
            'snippet':'降雨', 'chunks':[{'text':'明天有雨，建议携带雨具'}]}]}


async def test_search_evidence_returns_to_model_with_context():
    sdk = Fake([Message(calls=[call()]), Message('明天有雨，带伞。')])
    answer = await run_search_agent(sdk, 'kimi-k2.6',
        [{'role':'user', 'content':'我在上海'}, {'role':'user', 'content':'明天要带伞吗？'}],
        lambda: None, voice=False)
    assert answer.startswith('明天有雨，带伞。')
    assert 'https://weather.example' in answer
    assert sdk.searches[0]['text_query'] == '上海明天天气'
    tool = next(m for m in sdk.requests[-1]['messages'] if m['role'] == 'tool')
    assert tool['role'] == 'tool' and tool['tool_call_id'] == 'tool1'
    assert 'weather.example' in tool['content'] and '雨具' in tool['content']


async def test_normal_chat_does_not_search():
    sdk = Fake([Message('你好')])
    assert await run_search_agent(sdk, 'kimi-k2.6', [], lambda: None, voice=True) == '你好'
    assert not sdk.searches


@pytest.mark.parametrize('tool', [call(name='poweroff'), call(query=''), call(query='x'*301)])
async def test_invalid_tool_never_executes(tool):
    sdk = Fake([Message(calls=[tool]), Message('无法查询')])
    await run_search_agent(sdk, 'kimi-k2.6', [], lambda: None, voice=False)
    assert not sdk.searches


async def test_repeated_query_only_searched_once():
    sdk = Fake([Message(calls=[call()]), Message(calls=[call()]), Message('回答')])
    await run_search_agent(sdk, 'kimi-k2.6', [], lambda: None, voice=False)
    assert len(sdk.searches) == 1


async def test_privacy_blocks_all_requests():
    sdk = Fake([])
    def blocked():
        raise PrivacyBlocked()
    with pytest.raises(PrivacyBlocked):
        await run_search_agent(sdk, 'kimi-k2.6', [], blocked, voice=False)
    assert not sdk.requests and not sdk.searches


async def test_privacy_changed_after_model_blocks_search():
    sdk = Fake([Message(calls=[call()])])
    def check():
        if sdk.requests:
            raise PrivacyBlocked()
    with pytest.raises(PrivacyBlocked):
        await run_search_agent(sdk, 'kimi-k2.6', [], check, voice=False)
    assert not sdk.searches


async def test_search_connection_failure_does_not_invent_evidence():
    from openai import APIConnectionError
    import httpx2
    sdk = Fake([Message(calls=[call()]), Message('虚构的实时天气')])
    async def failed(*args, **kwargs):
        raise APIConnectionError(request=httpx2.Request('POST', 'https://unit.test'))
    sdk.post = failed
    reply = await run_search_agent(sdk, 'kimi-k2.6', [], lambda: None, voice=False)
    assert '未获得可核实' in reply and '虚构' not in reply


async def test_cancel_stops_pending_search():
    import asyncio
    sdk = Fake([Message(calls=[call()])])
    entered = asyncio.Event()
    stopped = asyncio.Event()
    async def pending(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    sdk.post = pending
    task = asyncio.create_task(run_search_agent(sdk, 'kimi-k2.6', [], lambda: None, voice=True))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_voice_progress_is_context_and_success_intro_only_after_evidence():
    sdk = Fake([Message(calls=[call()]), Message('明天有雨。')])
    progress = []
    answer = await run_search_agent(sdk, 'kimi-k2.6',
        [{'role':'user','content':'上海明天天气怎么样？'}], lambda: None,
        voice=True, on_progress=progress.append)
    assert progress == ['我帮你看看天气啊。']
    assert answer == '明天有雨。'
    assert any('已向用户播报' in str(m.get('content')) for m in sdk.requests[-1]['messages'])
    layout = sdk.requests[-1]['messages']
    assert layout[0]['role'] == 'system'
    assert all(m['role'] != 'system' for m in layout[1:])
    index = next(i for i,m in enumerate(layout) if m.get('tool_calls'))
    assert layout[index+1]['role'] == 'tool'


@pytest.mark.parametrize('status,expected', [(400,1),(401,1),(429,2),(503,2)])
async def test_retry_only_transient_errors(status, expected):
    import httpx2
    from openai import APIStatusError
    from lira.llm.search_agent import retry_remote
    calls = []
    async def operation():
        calls.append(1)
        raise APIStatusError('provider error', response=httpx2.Response(status,
            request=httpx2.Request('POST','https://unit.test')), body={})
    with pytest.raises(APIStatusError):
        await retry_remote(operation,lambda: None)
    assert len(calls) == expected


async def test_retry_checks_privacy_again():
    import httpx2
    from openai import APIConnectionError
    from lira.llm.search_agent import retry_remote
    calls=[]
    def privacy():
        if calls:
            raise PrivacyBlocked()
    async def operation():
        calls.append(1)
        raise APIConnectionError(request=httpx2.Request('POST','https://unit.test'))
    with pytest.raises(PrivacyBlocked):
        await retry_remote(operation,privacy)
    assert len(calls)==1
