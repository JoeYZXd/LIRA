import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessage

from lira.dictation_agent import DictationAgent
from test_dictation_service import service


@pytest.mark.asyncio
async def test_clarification_uses_real_service_and_stops_after_one_task(service):
    calls = []
    replies = [
        {'role': 'assistant', 'content': '请问默写第几课？'},
        {'role': 'assistant', 'tool_calls': [{'id': 'context', 'type': 'function',
          'function': {'name': 'get_dictation_context', 'arguments': json.dumps({'scope_type': 'lesson', 'start': 2})}}]},
        {'role': 'assistant', 'tool_calls': [{'id': 'prepare', 'type': 'function',
          'function': {'name': 'prepare_dictation_task', 'arguments': json.dumps({'proposals': {'2': {'words': ['尽头', '依靠'], 'reason': '覆盖本课'}}})}}]},
    ]

    async def create(**kwargs):
        calls.append(kwargs['messages'][:])
        return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(**replies.pop(0)))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = DictationAgent(client, 'test', service, owner_id=1, session_id='model-session', paired_device_id='tv', callback_registration_id='cb')
    assert (await agent.respond('开始默写'))['status'] == 'response'
    result = await agent.respond('第二课')
    assert result['status'] == 'ready'
    assert [t['tool'] for t in result['trace']] == ['get_dictation_context', 'prepare_dictation_task']
    assert [x['text'] for x in result['task']['phases'][1]['items']] == ['尽头', '依靠']
    assert (await agent.respond('再来一次'))['status'] == 'already_prepared'
    assert len(calls) == 3
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0] == 1
    assert any(m.get('role') == 'tool' and m.get('tool_call_id') == 'context' for m in calls[2])
