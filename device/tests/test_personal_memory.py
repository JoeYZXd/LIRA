import json
from types import SimpleNamespace
import numpy as np
import pytest
from lira.config import SpeakerConfig
from lira.personal_memory import PersonalMemory,SpeakerIdentity
from lira.speaker_candidates import VoiceCandidates
from lira.llm.search_agent import AgentReply

def memory(tmp_path, privacy=lambda:False):
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'summary':'喜欢简短的回答'})))])
    sdk=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return PersonalMemory(tmp_path/'device.db',sdk,'kimi-k2.6',privacy)


def test_voice_replacement_and_deletion_require_current_revision_preserve_member(tmp_path):
    m=memory(tmp_path)
    member=m.register_voice('原姓名','称呼',[1.,0.])
    revision=m.voice_revision(member)
    with pytest.raises(ValueError,match='变化'):
        m.save_guided_voice('原姓名','称呼',[.9,.4],replace_member_id=member,expected_revision='stale')
    np.testing.assert_array_equal(m.profiles()[0][1],[1.,0.])
    m.save_guided_voice('新姓名','新称呼',[.9,.4],replace_member_id=member,expected_revision=revision)
    with pytest.raises(ValueError,match='变化'): m.delete_voice_profile(member,revision)
    m.delete_voice_profile(member,m.voice_revision(member))
    assert not m.profiles() and m.people.get_member(member)['name']=='新姓名'


def test_replacement_rejects_another_members_name_or_voice(tmp_path):
    m=memory(tmp_path)
    first=m.register_voice('甲','甲',[1.,0.])
    m.register_voice('乙','乙',[0.,1.])
    revision=m.voice_revision(first)
    with pytest.raises(ValueError,match='姓名'):
        m.save_guided_voice('乙','甲',[1.,0.],replace_member_id=first,expected_revision=revision)
    with pytest.raises(ValueError,match='声音'):
        m.save_guided_voice('甲','甲',[0.,1.],replace_member_id=first,expected_revision=revision)
    assert m.voice_revision(first)==revision

def identity_with_candidates(tmp_path, privacy=lambda:False):
    """U7 生产形态：memory + identity + 候选共享同一 SQLite 连接（main 装配序）。"""
    m=memory(tmp_path,privacy)
    s=SpeakerIdentity(m,None,extractor=object())
    store=VoiceCandidates(m.db,fingerprint=s.model_fingerprint,profiles_provider=m.profiles,
                          params=SpeakerConfig(),privacy=privacy)
    s.candidates=store
    return m,s,store

def voice_samples(seconds=2.0, level=0.2):
    """过质量门的可控假音频（observe 用；内容与向量无关）。"""
    return np.full(int(16000*seconds),level,np.float32)


async def test_match_diagnostics_distinguish_threshold_margin_and_empty_profiles(tmp_path):
    m=memory(tmp_path)
    s=SpeakerIdentity(m,None,extractor=object(),threshold=.8,margin=.1)
    query=np.array([1.,0.],np.float32)
    assert s.match(query) is None
    assert s.last_match_details['reason']=='empty_profiles'
    first=m.enroll('甲',np.array([.79,np.sqrt(1-.79**2)],np.float32))
    assert s.match(query) is None
    assert s.last_match_details['reason']=='below_threshold'
    assert s.last_match_details['top_score']==pytest.approx(.79)
    assert s.last_match_details['threshold']==.8
    m.db.execute('UPDATE voice_profiles SET vector=? WHERE member_id=?',
                 (query.tobytes(),first));m.db.commit()
    assert s.match(query)==first
    assert s.last_match_details['reason']=='matched' and s.last_match_details['score_gap'] is None
    m.enroll('乙',np.array([.95,np.sqrt(1-.95**2)],np.float32))
    assert s.match(query) is None
    assert s.last_match_details['reason']=='insufficient_margin'
    assert s.last_match_details['score_gap']==pytest.approx(.05)
    assert s.last_match_details['required_margin']==.1
    assert not {'name','member_id','vector'} & s.last_match_details.keys()
    await m.close()


@pytest.mark.parametrize('profile',[np.array([1.,0.,0.],np.float32),np.array([np.nan,0.],np.float32)])
async def test_incompatible_profiles_report_fault_instead_of_a_match(tmp_path,profile):
    m=memory(tmp_path)
    m.enroll('损坏测试档案',profile)
    s=SpeakerIdentity(m,None,extractor=object())
    assert s.match(np.array([1.,0.],np.float32)) is None
    assert s.last_match_details['reason']=='incompatible_profiles'
    assert s.last_match_details['top_score'] is None
    await m.close()

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
    """U7 语义不变（新名流程）：同名说话人 + 候选命中才晋升；异声不登记。"""
    m,s,store=identity_with_candidates(tmp_path)
    one=np.array([1.,0.],np.float32);other=np.array([0.,1.],np.float32)
    store.observe(one,voice_samples(2.0,0.2))
    store.observe(other,voice_samples(2.0,0.3))
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
    """纯文本短称呼暂存到候选，等后续同一候选的有效声音再晋升（不凭空登记）。"""
    m,s,store=identity_with_candidates(tmp_path);one=np.array([1.,0.],np.float32)
    cid=store.observe(one,voice_samples()).candidate_id
    s.resolve('我想知道天气',one)
    s.pending['asked']=True
    member,notice,_=s.resolve('爸爸',None)
    assert member is None and not m.profiles()
    assert {c['candidate_id']:c for c in store.snapshot()['candidates']}[cid]['pending_name']=='爸爸'
    member,notice,replay=s.resolve('明天是不是也会下雨',one)
    # 锚定 _settle_pending 成功话术契约（U8 评审：合并重构后短名分支的
    # notice/replay 曾被 '_' 丢弃——回归将无报警）
    assert member is not None and notice is None and replay is None
    await m.close()

async def test_existing_text_member_requires_confirmation_and_never_replaces_voice(tmp_path):
    """E6 政策变更（U7，用户批准）：同名会话内"确认是我"**不再登记**——改为
    生成"同名待确认"项，仅鉴权控制台 confirm_association 能落声纹。
    强度保持：不登记 + 生成待确认项 + 控制台确认成功路径 + 既有声纹不覆盖。"""
    m,s,store=identity_with_candidates(tmp_path)
    expected=m.people.create_member('爸爸')['id']
    one=np.array([1.,0.],np.float32)
    cid=store.observe(one,voice_samples()).candidate_id
    s.resolve('查天气',one)
    member,notice,_=s.resolve('我是爸爸',one)
    assert member is None and '控制台' in notice, '同名只生成待确认项'
    row=store.snapshot()['candidates'][0]
    assert row['status']=='awaiting_console' and row['proposed_member_id']==expected
    assert m.profiles()==[], '确认前绝不写声纹（权限边界）'
    member,notice,_=s.resolve('确认是我',one)
    assert member is None and '控制台' in notice, '会话内确认不再登记'
    assert m.profiles()==[]
    result=store.confirm_association(cid,expected)
    assert result['ok'] is True and result['member_id']==expected
    assert s.match(one)==expected
    with pytest.raises(ValueError):m.enroll('爸爸',np.array([0.,1.],np.float32),allow_existing=True)
    await m.close()

async def test_ae4_same_name_claim_never_binds_and_memory_stays_closed(tmp_path):
    """AE4：说"我是妈妈"但存在同名成员时不绑定、不回忆——resolve 返回
    None 成员（main 因此保持 voice_member_id=None，recall/completed 全关）。"""
    import asyncio
    m,s,store=identity_with_candidates(tmp_path)
    mama=m.people.create_member('妈妈')['id']
    m.completed(mama,'喜欢茶','好的')
    await asyncio.gather(*m.tasks)
    one=np.array([1.,0.],np.float32)
    store.observe(one,voice_samples())
    s.resolve('查天气',one)
    member,notice,replay=s.resolve('我是妈妈',one)
    assert member is None and replay is None, '同名不绑定当前声音'
    assert '控制台' in notice
    assert m.recall(mama)!='', '妈妈记忆本身保留（由 voice_member_id 门控取用）'
    assert all(c['member_id'] is None for c in store.snapshot()['candidates']), \
        '确认前候选 member_id 恒 NULL（权限边界物理体现）'
    await m.close()

async def test_ae4_handle_voice_input_keeps_voice_member_id_none():
    """main 层 AE4：同名介绍话术路径不置 voice_member_id，真实问题可继续。"""
    from lira.main import DeviceRuntime
    from lira.dialog import DialogEngine,Router
    from test_state_machine import Recorder
    record=Recorder()
    async def answer(text,**kwargs):
        return '好的。'
    engine=DialogEngine(Router(remote_llm=answer,remote_available=lambda:True),record)
    await engine.on_wake()
    identity=SimpleNamespace(pending=None,embedding=lambda samples:np.array([1.,0.]),match=lambda v:None,
        resolve=lambda text,v:(None,'家里已有妈妈这个称呼。为避免认错人，这个关联需要在开发者控制台确认，确认前我不会读取这个身份的记忆。',None))
    runtime=SimpleNamespace(speaker_identity=identity,speaker_candidates=None,engine=engine,
        privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=engine.listening_generation),voice_member_id=None)
    await DeviceRuntime.handle_voice_input(runtime,'我是妈妈',np.ones(16000),runtime.dispatcher.generation,False)
    assert runtime.voice_member_id is None, '同名声称不得置位个人记忆取用门'
    assert record.spoken[-1]=='好的。', '普通自我介绍不再触发隐式注册'

async def test_new_name_confirmed_then_member_profile_stats_consistent(tmp_path):
    """自然新称呼确认后：profile/成员列表/统计同一事务一致；确认重试无重复成员。"""
    m,s,store=identity_with_candidates(tmp_path)
    one=np.array([1.,0.],np.float32)
    store.observe(one,voice_samples())
    s.resolve('帮我查天气',one)
    member,notice,replay=s.resolve('我是爸爸',one)
    assert member is not None and '爸爸' in notice and replay=='帮我查天气'
    assert [x['name'] for x in m.people.list_members()]==['爸爸']
    assert len(m.profiles())==1 and s.match(one)==member
    assert m.recall(member)!='', '确认后的 member_id 才能取用个人记忆'
    member2,notice2,_=s.resolve('我是爸爸',one)
    assert member2==member, '已建立声纹命中：不重复建成员'
    assert [x['name'] for x in m.people.list_members()]==['爸爸']
    await m.close()

async def test_misbind_switched_voice_never_receives_the_name(tmp_path):
    """误绑 case1：命名过程中换成另一候选——名字不晋升给错误声音。"""
    m,s,store=identity_with_candidates(tmp_path)
    one=np.array([1.,0.],np.float32);other=np.array([0.,1.],np.float32)
    cid1=store.observe(one,voice_samples(2.0,0.2)).candidate_id
    cid2=store.observe(other,voice_samples(2.0,0.3)).candidate_id
    assert cid1!=cid2
    s.resolve('查天气',one)
    member,_,_=s.resolve('我是爸爸',None)  # 纯文本报名字：暂存 cid1
    assert member is None
    rows={c['candidate_id']:c for c in store.snapshot()['candidates']}
    assert rows[cid1]['pending_name']=='爸爸'
    member,_,_=s.resolve('爸爸',other)  # 换人说话：同说话人检查失败 → 不晋升
    assert member is None and not m.profiles()
    rows={c['candidate_id']:c for c in store.snapshot()['candidates']}
    assert rows[cid1]['pending_name']=='爸爸' and rows[cid2]['pending_name'] is None
    await m.close()

async def test_misbind_ambiguous_voice_cannot_promote_staged_name(tmp_path):
    """误绑 case1'：声音与 pending 同人（cos≈0.906 过同说话人门）但候选归属
    歧义（对两候选边际为 0）→ lookup 返回 None → 不晋升。"""
    m,s,store=identity_with_candidates(tmp_path)
    rad=np.deg2rad
    one=np.array([1.,0.],np.float32)
    other=np.array([np.cos(rad(50.)),np.sin(rad(50.))],np.float32)
    amb=np.array([np.cos(rad(25.)),np.sin(rad(25.))],np.float32)
    store.observe(one,voice_samples(2.0,0.2))
    store.observe(other,voice_samples(2.0,0.3))
    s.resolve('查天气',one)
    s.resolve('我是爸爸',None)  # 文本称呼暂存 cid1
    member,notice,_=s.resolve('我是爸爸',amb)
    assert member is None and not m.profiles()
    assert all(c['status']!='promoted' for c in store.snapshot()['candidates'])
    await m.close()

async def test_misbind_privacy_flip_clears_staged_name(tmp_path):
    """误绑 case2：切隐私 → clear_voice_identity 清 pending 与候选暂存称呼，
    迟到的声音/回执不再拿这个名字晋升；voice_member_id 归零（记忆取用关闭）。"""
    from lira.main import DeviceRuntime
    m,s,store=identity_with_candidates(tmp_path)
    one=np.array([1.,0.],np.float32)
    cid=store.observe(one,voice_samples()).candidate_id
    s.resolve('查天气',one)
    s.resolve('我是爸爸',None)
    assert store.snapshot()['candidates'][0]['pending_name']=='爸爸'
    rt=SimpleNamespace(speaker_identity=s,speaker_candidates=store,voice_member_id=None)
    DeviceRuntime.clear_voice_identity(rt,True)  # 隐私开启订阅触发
    assert s.pending is None
    assert store.snapshot()['candidates'][0]['pending_name'] is None
    assert rt.voice_member_id is None
    assert not m.profiles()
    await m.close()

async def test_daily_input_does_not_replay_legacy_identity_question():
    from lira.main import DeviceRuntime
    from lira.dialog import DialogEngine,Router
    from test_state_machine import Recorder
    record=Recorder();asked=[]
    async def answer(text,**kwargs):
        asked.append(text);return '明天有雨。'
    engine=DialogEngine(Router(remote_llm=answer,remote_available=lambda:True),record)
    await engine.on_wake()
    identity=SimpleNamespace(embedding=lambda samples:np.array([1.,0.]),match=lambda v:1,
        resolve=lambda text,v:(1,'好，以后叫你爸爸。','帮我查明天天气'))
    runtime=SimpleNamespace(speaker_identity=identity,speaker_candidates=None,engine=engine,
        privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=engine.listening_generation),voice_member_id=None)
    await DeviceRuntime.handle_voice_input(runtime,'我是爸爸',np.ones(16000),runtime.dispatcher.generation,False)
    assert asked==['我是爸爸'] and runtime.voice_member_id==1

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
    identity=SimpleNamespace(pending={'text':'你是谁','asked':False},embedding=lambda samples:np.array([1.,0.]),match=lambda v:None,
        resolve=lambda text,v:(None,'我们好像还没认识过，你希望我怎么称呼你呀？',None))
    runtime=SimpleNamespace(speaker_identity=identity,speaker_candidates=None,engine=engine,
        privacy=SimpleNamespace(is_on=False),
        dispatcher=SimpleNamespace(generation=engine.listening_generation),voice_member_id=None)
    await DeviceRuntime.handle_voice_input(runtime,'你是谁',np.ones(16000),runtime.dispatcher.generation,False)
    assert asked==['你是谁']
    assert not any(x.startswith('我们好像还没认识过') for x in record.spoken)
    assert identity.pending['text']=='你是谁', '旧候选上下文不参与日常识别'

async def test_model_fingerprint_is_stable_and_dimension_aware(tmp_path):
    # U6/KTD5：候选表用指纹区分模型代次——路径+输出维度，跨重启稳定
    m=memory(tmp_path)
    s=SpeakerIdentity(m,None,extractor=object())
    # 跨实例稳定（U8 评审：原自比较恒真，未验证"两次独立构造同指纹"）
    again=SpeakerIdentity(m,None,extractor=object())
    assert s.model_fingerprint==again.model_fingerprint
    assert s.model_fingerprint.endswith(':0')  # 提取器无 dim 属性回退 0
    dimmed=SpeakerIdentity(m,None,extractor=SimpleNamespace(dim=192))
    assert dimmed.model_fingerprint.endswith(':192')
    assert dimmed.model_fingerprint!=s.model_fingerprint
    weird=SpeakerIdentity(m,None,extractor=SimpleNamespace(dim='not-a-number'))
    assert weird.model_fingerprint.endswith(':0')  # 非法 dim 安全回退
    await m.close()
