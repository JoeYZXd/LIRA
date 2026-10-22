import copy
import json
import sqlite3
from pathlib import Path
import pytest
from lira.dictation_task_contract import validate_task, content_hash, ContractError, compare_submission
from lira.dictation_settings import DictationSettings

FIXTURE = Path(__file__).parents[2] / 'tv-receiver/tests/fixtures/dictation-v1.json'

def task():
    value=json.loads(FIXTURE.read_text(encoding='utf-8'))
    value['content_hash']=content_hash(value)
    return value

def test_valid_snapshot_and_copy():
    value=task(); checked=validate_task(value)
    value['lessons'][0]['expanded_words'][0]['text']='changed'
    assert checked['lessons'][0]['expanded_words'][0]['text']=='依靠'

@pytest.mark.parametrize('mutate',[
    lambda t:t['parameters'].pop('expanded_timer_anchor'),
    lambda t:t['parameters'].update(expanded_word_seconds=-1),
    lambda t:t.update(schema_version=2),
    lambda t:t['lessons'][0]['expanded_words'][0].pop('audio_asset_id'),
    lambda t:t['lessons'][0]['expanded_words'][0].update(covers=[]),
    lambda t:t.update(callback_url='http://untrusted'),
    lambda t:t['parameters'].update(expanded_repeat_count=True),
])
def test_invalid_rejected_before_execution(mutate):
    value=task();mutate(value);value['content_hash']=content_hash(value)
    with pytest.raises(ContractError):validate_task(value)

def test_digest_and_unknown_capability():
    value=task();value['parameters']['expanded_word_seconds']=31
    with pytest.raises(ContractError,match='content_hash'):validate_task(value)
    with pytest.raises(ContractError,match='unsupported'):validate_task(task(),capabilities=[])

def test_config_requires_explicit_snapshot_and_cas():
    db=sqlite3.connect(':memory:');store=DictationSettings(db)
    assert store.get() is None
    params=task()['parameters']
    first=store.save(params,expected_revision=0)
    assert first['revision']==1
    with pytest.raises(ContractError,match='conflict'):store.save(params,expected_revision=0)
    params['expanded_word_seconds']=60
    assert store.get()['parameters']['expanded_word_seconds']==30
    assert store.save(params,expected_revision=1)['revision']==2

def test_submission_identity_and_frozen_configuration():
    original=task()
    assert compare_submission(None,original)=='new'
    assert compare_submission(original,task())=='existing'
    changed=task();changed['parameters']['advance_mode']='manual'
    changed['content_hash']=content_hash(changed)
    assert compare_submission(original,changed)=='conflict'
    changed['task_id']='other';changed['content_hash']=content_hash(changed)
    assert compare_submission(original,changed)=='different_task'

def test_multiline_recitation_and_focus_annotation():
    value=task();lesson=value['lessons'][0]
    lesson['recitation']={'text':'白日依山尽\n黄河入海流','prompt_audio_asset_id':'recitation-prompt'}
    lesson['focus_targets']=['登']
    value['content_hash']=content_hash(value)
    assert validate_task(value)['lessons'][0]['recitation']['text'].count('\n')==1
    lesson['focus_targets']=['不'];value['content_hash']=content_hash(value)
    with pytest.raises(ContractError,match='focus_targets'):validate_task(value)

def task_v2():
    return json.loads(FIXTURE.with_name('dictation-v2.json').read_text(encoding='utf-8'))

def test_v2_shared_fixture_and_capability():
    value=task_v2()
    assert validate_task(value,capabilities=['dictation_task_v2'])==value
    with pytest.raises(ContractError):validate_task(value,capabilities=['dictation_task_v1'])

@pytest.mark.parametrize('mutate',[
    lambda t:t.update(phases=[]),
    lambda t:t['phases'].append(copy.deepcopy(t['phases'][0])),
    lambda t:t['phases'][0].update(type='recitation'),
    lambda t:t['phases'][1]['items'][0].update(covers=[]),
    lambda t:t['phases'][0]['items'][0].update(text='河水'),
    lambda t:t.update(audio_assets=[]),
    lambda t:t['phases'][1]['items'][0].update(item_id='recognize-1'),
])
def test_v2_rejections(mutate):
    value=task_v2();mutate(value);value['content_hash']=content_hash(value)
    with pytest.raises(ContractError):validate_task(value)


def test_v2_recognition_displays_whole_words_with_target_coverage():
    value=task_v2();value['phases'][0]['items'][0]['text']='清水'
    value['content_hash']=content_hash(value)
    assert validate_task(value)['phases'][0]['items'][0]['text']=='清水'
