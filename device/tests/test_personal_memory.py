import json
from types import SimpleNamespace
import numpy as np
import pytest
from lira.personal_memory import PersonalMemory,SpeakerIdentity
from lira.llm.search_agent import AgentReply

def memory(tmp_path, privacy=lambda:False):
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'summary':'喜欢简短的回答'})))])
    sdk=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return PersonalMemory(tmp_path/'device.db',sdk,'kimi-k2.6',privacy)

async def test_completed_turns_persist_statistics_and_summary(tmp_path):
    m=memory(tmp_path);member=m.people.create_member('爸爸')['id']
    m.completed(member,'查天气',AgentReply('有雨',True))
    await __import__('asyncio').gather(*m.tasks)
    assert m.stats(member)['weather_queries_total']==1
    assert not m.stats(member)['habit_evidence_sufficient']
    assert '简短' in m.recall(member)
    await m.close()
    m=memory(tmp_path)
    assert m.stats(member)['weather_queries_total']==1
    await m.close()

async def test_identity_isolation_privacy_and_cascade(tmp_path):
    blocked=[False]
    m=memory(tmp_path,lambda:blocked[0]); a=m.people.create_member('爸爸')['id'];b=m.people.create_member('妈妈')['id']
    m.completed(a,'喜欢茶','好的')
    await __import__('asyncio').gather(*m.tasks)
    assert '喜欢茶' not in m.recall(b)
    blocked[0]=True
    m.completed(b,'查天气',AgentReply('有雨',True))
    assert m.stats(b)['weather_queries_total']==0 and m.recall(a)==''
    blocked[0]=False;m.people.delete_member(a)
    assert m.db.execute('SELECT COUNT(*) FROM memory_turns').fetchone()[0]==0
    await m.close()

async def test_passive_enrollment_requires_same_speaker_and_does_not_replace_name(tmp_path):
    m=memory(tmp_path);s=SpeakerIdentity(m,None,extractor=object())
    one=np.array([1.,0.],np.float32);other=np.array([0.,1.],np.float32)
    member,prompt,_=s.resolve('帮我查天气',one)
    assert member is None and '称呼' in prompt
    member,prompt,_=s.resolve('我是爸爸',other)
    assert member is None
    assert not m.profiles()
    s.pending=None;s.resolve('帮我查天气',one)
    member,notice,replay=s.resolve('我是爸爸',one)
    assert member is not None and replay=='帮我查天气'
    assert s.match(one)==member and s.match(other) is None
    with pytest.raises(ValueError):m.enroll('爸爸',other)
    await m.close()

async def test_short_name_waits_for_later_matching_ordinary_speech(tmp_path):
    m=memory(tmp_path);s=SpeakerIdentity(m,None,extractor=object());one=np.array([1.,0.],np.float32)
    s.resolve('我想知道天气',one)
    s.pending['asked']=True
    member,notice,_=s.resolve('爸爸',None)
    assert member is None and not m.profiles()
    member,_,_=s.resolve('明天是不是也会下雨',one)
    assert member is not None
    await m.close()

async def test_existing_text_member_requires_confirmation_and_never_replaces_voice(tmp_path):
    m=memory(tmp_path);expected=m.people.create_member('爸爸')['id'];s=SpeakerIdentity(m,None,extractor=object())
    one=np.array([1.,0.],np.float32)
    s.resolve('查天气',one)
    member,notice,_=s.resolve('我是爸爸',one)
    assert member is None and '关联' in notice
    member,_,_=s.resolve('确认是我',one)
    assert member==expected
    with pytest.raises(ValueError):m.enroll('爸爸',np.array([0.,1.],np.float32),allow_existing=True)
    await m.close()

async def test_identity_notice_replays_original_question_after_notice():
    from lira.main import DeviceRuntime
    from lira.dialog import DialogEngine,Router
    from test_state_machine import Recorder
    record=Recorder();asked=[]
    async def answer(text,**kwargs):
        asked.append(text);return '明天有雨。'
    engine=DialogEngine(Router(remote_llm=answer,remote_available=lambda:True),record)
    await engine.on_wake()
    identity=SimpleNamespace(embedding=lambda samples:np.array([1.,0.]),
        resolve=lambda text,v:(1,'好，以后叫你爸爸。','帮我查明天天气'))
    runtime=SimpleNamespace(speaker_identity=identity,engine=engine,privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=engine.listening_generation),voice_member_id=None)
    await DeviceRuntime.handle_voice_input(runtime,'我是爸爸',np.ones(16000),runtime.dispatcher.generation,False)
    assert asked==['帮我查明天天气'] and runtime.voice_member_id==1

async def test_erase_is_not_undone_by_inflight_summary(tmp_path):
    import asyncio
    m=memory(tmp_path);member=m.people.create_member('爸爸')['id'];entered=asyncio.Event();release=asyncio.Event()
    async def delayed(**kwargs):
        entered.set();await release.wait()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"旧偏好"}'))])
    m.sdk.chat.completions.create=delayed
    m.completed(member,'喜欢茶','好的');await entered.wait()
    m.forget(member);release.set();await asyncio.gather(*m.tasks)
    assert not m.db.execute('SELECT 1 FROM personal_memories').fetchone()
    assert m.stats(member)['weather_queries_total']==0
    await m.close()

async def test_yes_or_weather_is_not_enrolled_as_name(tmp_path):
    m=memory(tmp_path);s=SpeakerIdentity(m,None,extractor=object());one=np.array([1.,0.],np.float32)
    s.resolve('查天气',one)
    for text in ('是的','天气','你好'):
        member,_,_=s.resolve(text,one)
        assert member is None and not m.profiles()
    await m.close()

async def test_question_after_name_prompt_is_not_a_name(tmp_path):
    m=memory(tmp_path);s=SpeakerIdentity(m,None,extractor=object());one=np.array([1.,0.],np.float32)
    s.resolve('你是谁',one);s.pending['asked']=True
    for text in ('你是谁','我是谁','我是怎么回事','帮我查天气'):
        member,_,_=s.resolve(text,one)
        assert member is None and not m.profiles()
    await m.close()

async def test_new_speaker_question_is_answered_without_blocking_for_name():
    from lira.main import DeviceRuntime
    from lira.dialog import DialogEngine,Router
    from test_state_machine import Recorder
    record=Recorder();asked=[]
    async def answer(text,**kwargs):
        asked.append(text);return '我是小鹿，可以陪你聊天、查资料。对了，我该怎么称呼你呀？'
    engine=DialogEngine(Router(remote_llm=answer,remote_available=lambda:True),record)
    await engine.on_wake()
    identity=SimpleNamespace(pending={'text':'你是谁','asked':False},embedding=lambda samples:np.array([1.,0.]),
        resolve=lambda text,v:(None,'我们好像还没认识过，你希望我怎么称呼你呀？',None))
    runtime=SimpleNamespace(speaker_identity=identity,engine=engine,privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=engine.listening_generation),voice_member_id=None)
    await DeviceRuntime.handle_voice_input(runtime,'你是谁',np.ones(16000),runtime.dispatcher.generation,False)
    assert asked==['你是谁']
    assert not any(x.startswith('我们好像还没认识过') for x in record.spoken)
    assert identity.pending['text'] is None
