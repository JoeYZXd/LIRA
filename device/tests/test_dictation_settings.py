import sqlite3
import pytest
from lira.dictation_settings import DictationSettings

def parameters():
    return {'phases':['recognition','dictation'], 'playback':{'repeat_count':2,'interval_ms':3000,'speech_rate':1.0}}

def test_v2_revision_history_and_conflict():
    settings = DictationSettings(sqlite3.connect(':memory:'))
    assert settings.get_v2() is None
    first = settings.save_v2(parameters(), expected_revision=0)
    changed = parameters(); changed['phases'].reverse()
    assert settings.save_v2(changed, expected_revision=1)['revision'] == 2
    assert settings.get_v2(1) == first
    with pytest.raises(ValueError, match='conflict'):
        settings.save_v2(changed, expected_revision=1)

@pytest.mark.parametrize('patch', [{'phases':[]},{'phases':['recitation']},{'phases':['dictation','dictation']},{'font_size':50},{'auto_advance':True},{'playback':{'repeat_count':0,'interval_ms':0,'speech_rate':1}}])
def test_v2_rejects_unsupported_or_invalid_parameters(patch):
    value = parameters(); value.update(patch)
    with pytest.raises(ValueError):
        DictationSettings(sqlite3.connect(':memory:')).save_v2(value, expected_revision=0)

@pytest.mark.parametrize('key,value', [('repeat_count',True),('repeat_count',6),('interval_ms',-1),('interval_ms',600001),('speech_rate',True),('speech_rate',0.49),('speech_rate',float('nan')),('speech_rate',float('inf'))])
def test_playback_bounds(key,value):
    settings = DictationSettings(sqlite3.connect(':memory:'))
    config = parameters(); config['playback'][key] = value
    with pytest.raises(ValueError):
        settings.save_v2(config,expected_revision=0)

def test_save_v2_preserves_outer_transaction_and_v1():
    db = sqlite3.connect(':memory:')
    settings = DictationSettings(db)
    db.execute('BEGIN')
    settings.save_v2(parameters(),expected_revision=0)
    db.rollback()
    assert settings.get_v2() is None
    assert settings.get() is None


def test_cloud_voice_and_manual_navigation_are_versioned():
    settings=DictationSettings(sqlite3.connect(':memory:'))
    p=parameters();p['playback'].update(manual_navigation=True,repeat_count=1)
    p['tts']=dict(provider='doubao',resource_id='seed-tts-2.0',speaker='zh_female_vv_uranus_bigtts',pitch=0,speech_rate=-20,loudness_rate=30,instruction='朗读 播音 拉长音 着重强调 标准普通话')
    first=settings.save_v2(p,expected_revision=0)
    p['tts']['speech_rate']=-30
    settings.save_v2(p,expected_revision=1)
    assert settings.get_v2(1)==first
    assert first['parameters']['tts']['speech_rate']==-20
    assert settings.get_v2()['parameters']['tts']['speech_rate']==-30
    p['playback']['repeat_count']=2
    with pytest.raises(ValueError):settings.save_v2(p,expected_revision=2)
