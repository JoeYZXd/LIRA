import asyncio
import json
from types import SimpleNamespace
import pytest
import numpy as np
from lira.voice_enrollment import VoiceEnrollment
from lira.config import SpeakerConfig
from test_personal_memory import memory

V=np.array([1.,0.],np.float32)
def audio(seed=1):
    return np.random.default_rng(seed).normal(0,.1,64000).astype(np.float32)

def setup(tmp_path, privacy=lambda:False):
    m=memory(tmp_path,privacy)
    async def create(**kwargs):
        assert kwargs['stream'] is False
        assert 'tools' not in kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'name':'张三','address':'小张'})))])
    m.sdk.chat.completions.create=create
    e=VoiceEnrollment(m,SpeakerConfig());e.start('owner')
    return m,e

async def collect(e):
    await e.handle('我叫张三，叫我小张',audio(),V,current=lambda:True)
    for seed in (1,2,3):
        await e.handle('今天过得很好',audio(seed),V,current=lambda:True)

async def test_explicit_registration_atomic_and_requires_confirmation(tmp_path):
    m,e=setup(tmp_path)
    await collect(e)
    assert e.phase=='confirm' and not m.profiles()
    prompt,member=await e.handle('我确认按这个姓名和称呼保存',audio(4),V,current=lambda:True)
    assert member and '已经保存' in prompt and not e.active
    assert m.preferred_address(member)=='小张'
    assert json.loads(m.recall(member))['preferred_address']=='小张'
    await m.close()

async def test_confirmation_requires_same_voice_and_explicit_words(tmp_path):
    m,e=setup(tmp_path);await collect(e)
    for text,vector,samples in [('好的',V,audio()),('确认',np.array([0.,1.]),audio()),('确认',V,np.zeros(100))]:
        _,member=await e.handle(text,samples,vector,current=lambda:True)
        assert member is None and m.profiles()==[]
    await m.close()

async def test_duplicate_and_bad_samples_do_not_advance(tmp_path):
    m,e=setup(tmp_path)
    await e.handle('姓名',audio(),V,current=lambda:True)
    for samples,vector in [(audio(),V),(audio(),V),(audio(2),np.array([float('nan'),0.])),(audio(3),np.array([0.,1.]))]:
        await e.handle('一段话',samples,vector,current=lambda:True)
    assert len(e.vectors)==1 and not m.profiles()
    e.cancel();assert not e.vectors
    await m.close()

async def test_existing_name_or_voice_never_gains_or_duplicates_identity(tmp_path):
    m,e=setup(tmp_path);existing=m.enroll('张三',V)
    prompt,member=await e.handle('我叫张三',audio(),V,current=lambda:True)
    assert member is None and '控制台' in prompt and not e.active
    with pytest.raises(ValueError):m.register_voice('李四','小李',V)
    assert len(m.profiles())==1 and m.profiles()[0][0]==existing
    await m.close()

async def test_privacy_cancellation_interrupts_sdk_without_writes(tmp_path):
    blocked=[False];m,e=setup(tmp_path,lambda:blocked[0]);entered=asyncio.Event();cancelled=asyncio.Event()
    async def create(**kwargs):
        entered.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    m.sdk.chat.completions.create=create
    task=asyncio.create_task(e.handle('名字',audio(),V,current=lambda:True))
    await entered.wait();blocked[0]=True;e.cancel('owner')
    with pytest.raises(asyncio.CancelledError):await task
    assert cancelled.is_set() and not e.active and not m.profiles()
    await m.close()

async def test_stale_owner_and_timeout_never_commit(tmp_path):
    m,e=setup(tmp_path);await collect(e)
    _,member=await e.handle('确认',audio(),V,current=lambda:False)
    assert member is None and not m.profiles()
    e.start('next');e.started-=241
    await e.handle('名字',audio(),V,current=lambda:True)
    assert not e.active and not m.profiles()
    await m.close()

async def test_database_failure_rolls_back_member_and_profile(tmp_path):
    m,e=setup(tmp_path)
    m.db.execute("CREATE TRIGGER fail_address BEFORE INSERT ON voice_preferences BEGIN SELECT RAISE(ABORT,'test'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):m.register_voice('张三','小张',V)
    assert not m.people.list_members() and not m.profiles()
    await m.close()

async def test_runtime_invites_once_after_answer_and_requires_explicit_consent(tmp_path):
    from lira.main import DeviceRuntime,DeviceCallbacks
    from lira.dialog import DialogEngine,Router
    from test_state_machine import Recorder
    m,e=setup(tmp_path);e.cancel()
    contexts=[];requests=[];record=Recorder()
    async def answer(text,**kwargs):
        requests.append(text);contexts.append(DeviceRuntime.voice_identity_context(rt));return '我是LIRA，可以聊天。'
    engine=DialogEngine(Router(remote_llm=answer,remote_available=lambda:True),record)
    rt=SimpleNamespace(speaker_identity=SimpleNamespace(embedding=lambda samples:V,match=lambda v:None),
        voice_enrollment=e,voice_member_id=None,personal_memory=m,engine=engine,privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=0))
    rt.tts=None
    record.cancel_audio=lambda owner:DeviceCallbacks(rt).cancel_audio(owner)
    await engine.on_wake()
    async def say(text):
        rt.dispatcher.generation=engine.listening_generation
        await DeviceRuntime.handle_voice_input(rt,text,audio(),rt.dispatcher.generation,False)
    await say('你是谁')
    assert requests==['你是谁'] and '完整回答用户当前请求' in contexts[-1]
    assert not e.active and not m.profiles()
    await say('好的')
    assert not e.active and contexts[-1]==''
    # A fresh explicit request remains available even after ignoring the invitation.
    await say('录入声纹')
    assert e.active and e.phase=='details' and requests==['你是谁','好的']
    await say('我叫张三，称呼我小张')
    assert e.phase=='samples'
    for seed in (1,2,3):
        rt.speaker_identity.embedding=lambda samples:V
        rt.dispatcher.generation=engine.listening_generation
        await DeviceRuntime.handle_voice_input(rt,'今天的计划',audio(seed),rt.dispatcher.generation,False)
    assert e.phase=='confirm' and not m.profiles()
    await say('我确认按这个姓名和称呼保存')
    assert rt.voice_member_id and not e.active
    assert '小张' in DeviceRuntime.voice_identity_context(rt)
    await m.close()

async def test_runtime_invitation_explicit_consent_enters_without_extra_model_call(tmp_path):
    from lira.main import DeviceRuntime
    from lira.dialog import DialogEngine,Router
    from test_state_machine import Recorder
    m,e=setup(tmp_path);e.cancel()
    record=Recorder()
    async def answer(text,**kwargs):
        DeviceRuntime.voice_identity_context(rt);return '你好，愿意注册声纹吗？'
    engine=DialogEngine(Router(remote_llm=answer,remote_available=lambda:True),record)
    rt=SimpleNamespace(speaker_identity=SimpleNamespace(embedding=lambda samples:V,match=lambda v:None),
        voice_enrollment=e,voice_member_id=None,personal_memory=m,engine=engine,privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=0))
    await engine.on_wake()
    for text in ('你好','愿意注册'):
        rt.dispatcher.generation=engine.listening_generation
        await DeviceRuntime.handle_voice_input(rt,text,audio(),rt.dispatcher.generation,False)
    assert e.active and e.phase=='details' and not m.profiles()
    await m.close()

@pytest.mark.parametrize('payload',[{'name':'张三','address':5},{'name':'张三','address':'   '},[],{'name':'张三\n坏','address':'小张'}])
async def test_invalid_model_fields_do_not_partially_update_or_save(tmp_path,payload):
    m,e=setup(tmp_path)
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])
    m.sdk.chat.completions.create=create
    await e.handle('资料',audio(),V,current=lambda:True)
    assert e.name=='' and e.address=='' and e.phase=='details' and not m.profiles()
    await m.close()

async def test_wake_callback_cancels_registration_and_clears_identity_ram(tmp_path):
    from lira.main import DeviceCallbacks
    m,e=setup(tmp_path);e.name='张三';e.address='小张';e.vectors=[V]
    rt=SimpleNamespace(voice_enrollment=e,tts=None,_voice_invitation_owner='owner',_voice_invitation_context=True)
    DeviceCallbacks(rt).cancel_audio('other-owner')
    assert e.active
    DeviceCallbacks(rt).cancel_audio('owner')
    assert not e.active and e.name==e.address=='' and not e.vectors
    assert rt._voice_invitation_owner is None and not rt._voice_invitation_context
    await m.close()

async def test_expired_invitation_cannot_start_registration(tmp_path):
    from lira.main import DeviceRuntime
    from lira.dialog import DialogEngine,Router
    from test_state_machine import Recorder
    m,e=setup(tmp_path);e.cancel();calls=[]
    async def answer(text,**kwargs):calls.append(text);return '好的。'
    engine=DialogEngine(Router(remote_llm=answer,remote_available=lambda:True),Recorder());await engine.on_wake()
    rt=SimpleNamespace(speaker_identity=SimpleNamespace(embedding=lambda samples:V,match=lambda v:None),voice_enrollment=e,
        voice_member_id=None,personal_memory=m,engine=engine,privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=engine.listening_generation),_voice_invitation_owner=engine._owner,
        _voice_invitation_time=0,_voice_invitation_offered=True)
    await DeviceRuntime.handle_voice_input(rt,'愿意注册',audio(),rt.dispatcher.generation,False)
    assert not e.active and calls==['愿意注册']
    await m.close()

async def test_parser_timeout_and_offline_never_create_partial_identity(tmp_path):
    m,e=setup(tmp_path)
    async def create(**kwargs):raise asyncio.TimeoutError
    m.sdk.chat.completions.create=create
    prompt,member=await e.handle('姓名资料',audio(),V,current=lambda:True)
    assert member is None and '姓名' in prompt and not m.people.list_members()
    m.network_ok=lambda:False
    await e.handle('姓名资料',audio(),V,current=lambda:True)
    assert not e.active and not m.profiles()
    await m.close()
