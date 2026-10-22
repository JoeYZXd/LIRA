import numpy as np
import pytest
import json
import sqlite3
from pathlib import Path
from lira.dictation_assets import pcm24
from lira.dictation_preparation import choose_words,DictationPreparation

def test_coverage_subset_rotation():
    assert choose_words(['依'], ['依靠','依旧'], ['依靠']) == ['依旧']
    assert choose_words(['依','靠'], ['依靠','依旧'], []) == ['依靠']
    with pytest.raises(ValueError): choose_words(['依','尽'], ['依靠'], [])

def test_pcm_resamples_invalid_channels_and_nan():
    assert len(pcm24(np.ones(16000, dtype=np.float32)*0.5,16000)) == 48000
    with pytest.raises(ValueError): pcm24(np.array([float('nan')]),16000)
    with pytest.raises(ValueError): pcm24(np.ones((3,2)),16000)

def test_downsample_filters_above_nyquist():
    signal=np.sin(2*np.pi*18000*np.arange(48000)/48000).astype('float32')
    out=np.frombuffer(pcm24(signal,48000),dtype='<i2').astype(float)/32767
    assert len(out)==24000
    assert np.sqrt(np.mean(out[100:-100]**2))<.02

def service(tmp_path):
    from lira.textbook_practice import TextbookPractice
    from lira.dictation_settings import DictationSettings
    from lira.dictation_repository import DictationRepository
    root=tmp_path/'imports';folder=root/('a'*32);folder.mkdir(parents=True)
    rows=[dict(unit=4,number=7,title='古诗',reviewed=True,four=['依','尽'],two=['登'],expansions='依：依靠、依旧；尽：尽头',text='白日依山尽'),dict(unit=7,number=18,title='课文',reviewed=False,four=['山'],two=[]),dict(unit=7,number=19,title='课文二',reviewed=True,four=['人'],two=[])]
    draft=dict(id='a'*32,title='语文',status='review',revision=4,lessons=rows)
    (folder/'draft.json').write_text(json.dumps(draft),encoding='utf8')
    db=sqlite3.connect(':memory:');settings=DictationSettings(db)
    fixture=json.loads((Path(__file__).parents[2]/'tv-receiver/tests/fixtures/dictation-v1.json').read_text(encoding='utf8'))
    settings.save(fixture['parameters'],expected_revision=0)
    return DictationPreparation(TextbookPractice(tmp_path/'books',draft_directory=root),settings,DictationRepository(db)),folder

def test_real_draft_scopes_not_published_fullbook_and_freeze(tmp_path):
    p,folder=service(tmp_path)
    args=dict(scope_type='lesson',start=7,proposals={'7':dict(recognition_words=['登山'],pinyin_required_for=['依'],include_recitation=True,focus_nasal=True)})
    result=p.prepare(args,callback_registration_id='paired')
    assert result['status']=='ready'
    lesson=result['task']['lessons'][0]
    assert lesson['source']['revision']==4
    assert [w['text'] for w in lesson['expanded_words']]==['依靠','尽头']
    assert lesson['expanded_words'][0]['pinyin_required_for']==['依']
    assert lesson['recognition_words']==[dict(text='登山',covers=['登'])]
    assert '登' in lesson['focus_targets']
    assert lesson['recitation']['text']=='白日依山尽'
    original=result['task']['content_hash']
    (folder/'draft.json').write_text('{}',encoding='utf8')
    assert result['task']['content_hash']==original

def test_unit_missing_review_reports_precise_catalog_no_silent_skip(tmp_path):
    p,_=service(tmp_path)
    result=p.prepare(dict(scope_type='unit',start=7),callback_registration_id='paired')
    assert result['missing_lessons']==[dict(unit=7,number=18)]
    assert 'task' not in result

def test_invalid_proposed_pinyin_answers_cannot_go_to_tv(tmp_path):
    p,_=service(tmp_path)
    result=p.prepare(dict(scope_type='lesson',start=7,proposals={'7':dict(recognition_words=['deng'])}),callback_registration_id='paired')
    assert result['status']=='word_proposals_required'

def test_reviewed_recitation_defaults_present_explicit_omit(tmp_path):
    p,_=service(tmp_path)
    args=dict(scope_type='lesson',start=7,character_group='four')
    result=p.prepare(args,callback_registration_id='paired')
    assert result['task']['lessons'][0]['recitation']['text']=='白日依山尽'
    args['proposals']={'7':{'include_recitation':False}}
    assert p.prepare(args,callback_registration_id='paired')['task']['lessons'][0]['recitation'] is None

def test_english_draft_not_chinese_task_source(tmp_path):
    p,folder=service(tmp_path)
    draft=json.loads((folder/'draft.json').read_text(encoding='utf8'));draft['subject']='english'
    (folder/'draft.json').write_text(json.dumps(draft),encoding='utf8')
    assert p.prepare(dict(scope_type='lesson',start=7,character_group='four'),callback_registration_id='paired')['status']=='needs_input'
