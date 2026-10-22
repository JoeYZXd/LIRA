import pytest
from test_teaching_memory import service
from lira.teaching_content import TeachingContentRepository
from lira.teaching_evidence import TeachingEvidence

@pytest.mark.asyncio
async def test_recitation_uses_local_asr_and_confirmed_reference_only():
    memory,task,identity,rt=service()
    content=TeachingContentRepository(rt.store._conn)
    draft=content.save_draft(title='诗',text='床前明月光',kind='poem',source='家长手工录入')
    evidence=TeachingEvidence(memory,content,lambda:[])
    args={'content_id':draft['id'],'version':draft['version'],'turn_id':task.turn['id']}
    assert (await evidence.compare(args))['status']=='draft'
    content.confirm(draft['id'],draft['version'])
    task.observe_user('床前月光');args['turn_id']=task.turn['id']
    result=await evidence.compare(args)
    assert result['status']=='ok'
    assert result['review_state']=='needs_review'
    assert result['evidence']['observed_transcript']=='床前月光'
    assert result['evidence']['differences']
    assert result['evidence']['reference_version']==1
    assert result['evidence']['delivered_speech']==[]
    assert (await evidence.compare(args))['id']==result['id']
    task.observe_user('下一句')
    assert (await evidence.compare(args))['status']=='needs_input'

@pytest.mark.asyncio
async def test_reference_limit_version_change_parent_correction_preserves_evidence():
    memory,task,identity,rt=service()
    content=TeachingContentRepository(rt.store._conn)
    draft=content.save_draft(title='诗',text='床前明月光',kind='poem',source='manual')
    content.confirm(draft['id'],1)
    evidence=TeachingEvidence(memory,content,lambda:[{'speech_id':'real','text':'床前','generation':'g','owner':'o','source':'exact_speech_drained','delivered_at':task.turn['observed_at']-1}])
    result=await evidence.compare({'content_id':draft['id'],'version':1,'turn_id':task.turn['id']})
    assert result['evidence']['hint_usage']=='unknown'
    assert result['evidence']['delivered_speech'][0]['speech_id']=='real'
    revised=memory.parent_annotate_recitation(member_id=identity.member_id,evidence_id=result['id'],expected_revision=1,request_id='fix',result='转写漏了明，孩子实际读了',note='家长人工核对')
    original=rt.store._conn.execute('select body from teaching_memories where id=?',(result['id'],)).fetchone()[0]
    import json
    assert json.loads(original)==result['evidence']
    assert revised['body']['result']=='转写漏了明，孩子实际读了'
    with pytest.raises(ValueError):
        memory.parent_review(member_id=identity.member_id,record_id=result['id'],expected_revision=1,request_id='bad',result='覆盖证据',note='')
    content.save_draft(title='诗',text='更新原文',kind='poem',source='manual',content_id=draft['id'],expected_version=1)
    assert (await evidence.compare({'content_id':draft['id'],'version':1,'turn_id':task.turn['id']}))['status']=='version_conflict'
    big=content.save_draft(title='长文',text='文'*8001,kind='text',source='manual');content.confirm(big['id'],1)
    assert (await evidence.compare({'content_id':big['id'],'version':1,'turn_id':task.turn['id']}))['reason']=='reference_exceeds_alignment_limit'

@pytest.mark.asyncio
async def test_content_purge_removes_all_members_evidence_and_annotations_only_for_target():
    memory,task,identity,rt=service()
    content=TeachingContentRepository(rt.store._conn)
    target=content.save_draft(title='甲',text='甲',kind='text',source='manual');content.confirm(target['id'],1)
    other=content.save_draft(title='乙',text='乙',kind='text',source='manual');content.confirm(other['id'],1)
    evidence=TeachingEvidence(memory,content,lambda:[])
    for member in (identity.member_id,rt.personal_memory.people.create_member('另一个孩子')['id']):
        identity.member_id=member
        for draft in (target,other):
            row=await evidence.compare({'content_id':draft['id'],'version':1,'turn_id':task.turn['id']})
            memory.parent_annotate_recitation(member_id=member,evidence_id=row['id'],expected_revision=1,request_id=f"{member}-{draft['id']}",result='家长核对',note='')
    assert memory.purge_content(target['id'])['deleted_records']==4
    assert rt.store._conn.execute('SELECT COUNT(*) FROM teaching_memories').fetchone()[0]==4
    assert rt.store._conn.execute('SELECT COUNT(*) FROM teaching_record_refs WHERE content_id=?',(target['id'],)).fetchone()[0]==0
    assert rt.store._conn.execute('SELECT COUNT(*) FROM teaching_record_refs WHERE content_id=?',(other['id'],)).fetchone()[0]==4

@pytest.mark.asyncio
async def test_future_speech_not_attached_to_past_observation_and_executor_completed():
    from lira.realtime_capabilities import CapabilityRegistry,CapabilityExecutor
    memory,task,identity,rt=service()
    content=TeachingContentRepository(rt.store._conn)
    row=content.save_draft(title='诗',text='床前明月光',kind='poem',source='manual');content.confirm(row['id'],1)
    evidence=TeachingEvidence(memory,content,lambda:[{'generation':'g','owner':'o','delivered_at':task.turn['observed_at']+1,'text':'未来提示'}])
    registry=CapabilityRegistry();evidence.register(registry);executor=CapabilityExecutor(registry)
    result=await executor.call('compare_observed_recitation',{'content_id':row['id'],'version':1,'turn_id':task.turn['id']},'recite')
    assert result['status']=='ok' and result['review_state']=='needs_review'
    assert result['evidence']['delivered_speech']==[]
    assert result['evidence']['hint_usage']=='unknown'
    assert (await executor.call('list_capability_operations',{},'list'))['operations'][0]['status']=='completed'
    await executor.close()
