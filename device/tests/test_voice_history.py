import json

import pytest

from lira.appliances.store import ApplianceStore
from lira.personal_memory import PersonalMemory
from lira.voice_history import VoiceHistory


def test_turn_details_keep_actual_payloads_redacted_and_separate_from_manual_ownership(tmp_path):
    store=ApplianceStore(tmp_path/'details.db');h=VoiceHistory(store)
    h.begin()
    h.trace('sent','session.create',{'session':{'instructions':'系统设定'},'api_key':'private-key'},turn=0)
    h.event('user_text','你好');h.event('assistant_text','你好呀');h.event('response_done')
    h.trace('local','voiceprint.progress',{'top_score':.635,'state':'collecting'},turn=1)
    h.trace('received','response.done',{'response':{'usage':{'tokens':42}}},turn=1)
    h.event('user_text','我是李四')
    # An older background lookup finishing later still belongs to the first turn.
    h.trace('local','voiceprint.lookup',{'top_score':.7},turn=1)
    sid=h.session_id;h.end()
    h=VoiceHistory(store);data=h.detail(sid)
    assert len(data['turns'][0]['diagnostics'])==3
    assert not data['turns'][1]['diagnostics']
    assert data['turns'][0]['diagnostics'][0]['payload']['top_score']==.635
    assert 'private-key' not in json.dumps(data)
    assert data['session_diagnostics'][0]['payload']['session']['instructions']=='系统设定'
    assert not data['turns'][0].get('member_id')


def test_secret_redaction_keeps_tool_schemas_and_decodes_nested_results():
    from lira.voice_trace import redact
    value={'token':{'type':'string','maxLength':40},'items':[{'text':json.dumps({'token':'feedback-receipt','api_key':'actual-key','name':'甲'})}],
           'instructions':'do not echo actual-key','audio':'AAAA'}
    safe=redact(value,('actual-key',))
    assert safe['token']['type']=='string'
    assert json.loads(safe['items'][0]['text'])['name']=='甲'
    assert 'actual-key' not in json.dumps(safe) and 'feedback-receipt' not in json.dumps(safe)
    assert safe['audio']!='AAAA'


def test_history_records_real_turns_and_manual_links_are_idempotent_reassignable(tmp_path):
    store=ApplianceStore(tmp_path/'history.db')
    h=VoiceHistory(store)
    h.event('user_text','我想种番茄')
    sid=h.session_id
    h.event('assistant_text','记得浇水')
    h.event('response_done')
    h.event('user_text','下次继续聊')
    h.end()
    data=h.detail(sid)
    assert [t['state'] for t in data['turns']]==['completed','incomplete']
    m=PersonalMemory(tmp_path/'memory.db',None,'test',lambda:False)
    first=m.people.create_member('甲')['id'];second=m.people.create_member('乙')['id']
    m.link_voice_turns(sid,data['turns'][:1],first)
    m.link_voice_turns(sid,data['turns'][:1],first)
    assert len(json.loads(m.recall(first))['manually_linked_conversations'])==1
    m.link_voice_turns(sid,data['turns'][:1],second)
    assert not json.loads(m.recall(first))['manually_linked_conversations']
    assert '番茄' in m.recall(second)
    m.link_voice_turns(sid,data['turns'][:1],None)
    assert not m.voice_links(sid)
    assert len(h.detail(sid)['turns'])==2


def test_history_limit_and_interrupted_marker(tmp_path):
    store=ApplianceStore(tmp_path/'history.db');h=VoiceHistory(store)
    for i in range(102):
        h.begin();h.event('user_text',str(i));h.event('interrupted');h.end()
    assert len(h.sessions())==100
    latest=h.detail(h.sessions()[0]['id'])
    assert latest['turns'][0]['state']=='interrupted'


def test_history_api_requires_consent_and_protects_privacy(tmp_path):
    from test_dev_console import make_voice_memory_client,login
    from types import SimpleNamespace
    _,client,rt=make_voice_memory_client(tmp_path)
    h=VoiceHistory(rt.store);rt.conversation_paths=SimpleNamespace(history=h)
    h.event('user_text','明年想去旅行');h.event('assistant_text','去哪里呢');h.event('response_done');h.end()
    session=h.sessions()[0]['id'];turn=h.detail(session)['turns'][0]['id']
    member=rt.personal_memory.people.create_member('成员')['id']
    url='/dev/api/voice-history/'+session
    payload=dict(confirm=True,member_id=member,turn_ids=[turn])
    assert client.get(url).status_code==401
    login(client)
    assert client.post(url,json={**payload,'confirm':False}).status_code==400
    assert client.post(url,json={**payload,'turn_ids':[turn+99]}).status_code==400
    assert client.post(url,json=payload).status_code==200
    assert '旅行' in rt.personal_memory.recall(member)
    assert client.get(url+'/export').status_code==200
    rt.privacy._on=True
    assert client.get(url).status_code==403
    assert client.get('/dev/api/voice-history').status_code==403
    assert client.post(url,json=payload).status_code==403
