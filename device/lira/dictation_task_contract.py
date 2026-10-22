"""Version-one immutable dictation wire contract; no execution or authorization."""
from __future__ import annotations
import copy
import hashlib
import json
import re

CAPABILITY = 'dictation_task_v1'
MAX_TASK_BYTES = 262144
PARAMETERS = {
    'advance_mode': ('automatic','manual'),
    'expanded_word_seconds': (1,600),
    'expanded_repeat_count': (1,5),
    'expanded_timer_anchor': ('after_playback','from_item_start'),
    'recognition_seconds_per_character': (1,600),
    'resume_timer_policy': ('restart_current','remaining'),
    'recitation_end_mode': ('observed_user_signal','remote_confirm'),
}
class ContractError(ValueError):
    pass

def fail(path, reason):
    raise ContractError(f'{path}: {reason}')

def obj(value, keys, path):
    if type(value) is not dict or set(value)!=set(keys):fail(path,'missing or unknown fields')

def integer(value, low, high, path):
    if type(value) is not int or not low<=value<=high:fail(path,'integer out of bounds')

def text(value, limit, path, *, identifier=False):
    if type(value) is not str or not 0<len(value)<=limit or any(ord(c)<32 for c in value):fail(path,'invalid text')
    if identifier and not re.fullmatch(r'[A-Za-z0-9_.:-]+',value):fail(path,'invalid identifier')

def validate_parameters(value):
    obj(value,PARAMETERS,'parameters')
    for key,allowed in PARAMETERS.items():
        v=value[key]
        if type(allowed[0]) is int:integer(v,*allowed,'parameters.'+key)
        elif type(v) is not str or v not in allowed:fail('parameters.'+key,'unsupported value')
    return copy.deepcopy(value)

def canonical_json(value):
    def normalize(v):
        if type(v) is float and v.is_integer():return int(v)
        if type(v) is dict:return {k:normalize(x) for k,x in v.items()}
        if type(v) is list:return [normalize(x) for x in v]
        return v
    return json.dumps(normalize(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def content_hash(value):
    # Identity, config, callback registration and every lesson field are frozen together.
    snapshot={k:v for k,v in value.items() if k!='content_hash'}
    return hashlib.sha256(canonical_json(snapshot).encode('utf-8')).hexdigest()

def validate_task(value, *, capabilities=None):
    if type(value) is dict and value.get('schema_version')==2:return validate_task_v2(value,capabilities=capabilities)
    if capabilities is not None and CAPABILITY not in capabilities:fail('capabilities','unsupported dictation_task_v1')
    obj(value,('schema_version','task_id','content_hash','config_revision','lessons','parameters','callback_registration_id'),'task')
    integer(value['schema_version'],1,1,'schema_version')
    for k in ('task_id','callback_registration_id'):text(value[k],128,k,identifier=True)
    integer(value['config_revision'],1,2147483647,'config_revision')
    validate_parameters(value['parameters'])
    lessons=value['lessons']
    if type(lessons) is not list or not 1<=len(lessons)<=50:fail('lessons','expected 1..50 lessons')
    ids=set()
    for n,lesson in enumerate(lessons):
        p=f'lessons[{n}]'
        obj(lesson,('lesson_id','source','four_targets','expanded_words','two_targets','recognition_words','recitation','focus_targets'),p)
        text(lesson['lesson_id'],128,p+'.lesson_id',identifier=True)
        if lesson['lesson_id'] in ids:fail(p,'duplicate lesson')
        ids.add(lesson['lesson_id'])
        src=lesson['source'];obj(src,('book_id','unit','number','revision'),p+'.source')
        text(src['book_id'],128,p+'.source.book_id',identifier=True)
        for k in ('unit','number','revision'):integer(src[k],1,2147483647,p+'.source.'+k)
        for targets_key,words_key,audio in [('four_targets','expanded_words',True),('two_targets','recognition_words',False)]:
            targets=lesson[targets_key];words=lesson[words_key]
            if type(targets) is not list or len(targets)>200 or len(set(str(t) for t in targets))!=len(targets):fail(p+'.'+targets_key,'invalid targets')
            for t in targets:
                text(t,1,p+'.'+targets_key)
                if not '\u3400'<=t<='\u9fff':fail(p,'target must be Han character')
            if type(words) is not list or len(words)>200:fail(p+'.'+words_key,'invalid words')
            covered=set()
            for word in words:
                fields=('text','covers','audio_asset_id','pinyin_required_for') if audio else ('text','covers')
                obj(word,fields,p+'.'+words_key);text(word['text'],32,p+'.word.text')
                covers=word['covers']
                if type(covers) is not list or not covers or any(type(c) is not str for c in covers) or len(set(covers))!=len(covers):fail(p,'invalid covers')
                if any(c not in targets or c not in word['text'] for c in covers):fail(p,'covers must occur in word and targets')
                covered.update(covers)
                if audio:
                    text(word['audio_asset_id'],128,p+'.audio_asset_id',identifier=True)
                    req=word['pinyin_required_for']
                    if type(req) is not list or any(type(c) is not str or c not in covers for c in req) or len(set(req))!=len(req):fail(p,'invalid pinyin_required_for')
            if covered!=set(targets):fail(p,'incomplete target coverage')
        if not lesson['expanded_words'] and not lesson['recognition_words']:fail(p,'empty dictation lesson')
        focus=lesson['focus_targets']
        all_targets=lesson['four_targets']+lesson['two_targets']
        if type(focus) is not list or any(type(t) is not str or t not in all_targets for t in focus) or len(set(focus))!=len(focus):fail(p+'.focus_targets','invalid focus targets')
        rec=lesson['recitation']
        if rec is not None:
            obj(rec,('text','prompt_audio_asset_id'),p+'.recitation')
            text(rec['text'].replace('\n',' ').replace('\r',' ').replace('\t',' ') if type(rec['text']) is str else rec['text'],16000,p+'.recitation.text')
            text(rec['prompt_audio_asset_id'],128,p+'.recitation.prompt_audio_asset_id',identifier=True)
    digest=value['content_hash']
    if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest) or digest!=content_hash(value):fail('content_hash','snapshot mismatch')
    if len(canonical_json(value).encode('utf-8'))>MAX_TASK_BYTES:fail('task','payload too large')
    return copy.deepcopy(value)


def compare_submission(existing, incoming):
    """No side effects: persistence owner must compare and insert atomically."""
    validate_task(incoming)
    if existing is None:return 'new'
    if existing['task_id']!=incoming['task_id']:return 'different_task'
    return 'existing' if existing['content_hash']==incoming['content_hash'] else 'conflict'

V2_CAPABILITY = 'dictation_task_v2'

def validate_parameters_v2(value):
    if type(value) is not dict:fail('playback','expected object')
    obj(value,('repeat_count','interval_ms','speech_rate')+ (('manual_navigation',) if 'manual_navigation' in value else ()),'playback')
    if 'manual_navigation' in value and type(value['manual_navigation']) is not bool:fail('playback.manual_navigation','expected boolean')
    if value.get('manual_navigation') and value['repeat_count']!=1:fail('playback.repeat_count','manual navigation requires one playback')
    integer(value['repeat_count'],1,5,'playback.repeat_count')
    integer(value['interval_ms'],0,600000,'playback.interval_ms')
    rate=value['speech_rate']
    if type(rate) not in (int,float) or not 0.5<=rate<=2:fail('playback.speech_rate','expected 0.5..2')
    return copy.deepcopy(value)

def validate_task_v2(value, *, capabilities=None):
    if capabilities is not None and V2_CAPABILITY not in capabilities:fail('capabilities','unsupported dictation_task_v2; upgrade TV')
    obj(value,('schema_version','task_id','content_hash','config_revision','skill_revision','callback_registration_id','lessons','phases','audio_assets','playback'),'task')
    integer(value['schema_version'],2,2,'schema_version')
    for k in ('task_id','callback_registration_id'):text(value[k],128,k,identifier=True)
    for k in ('config_revision','skill_revision'):integer(value[k],1,2147483647,k)
    validate_parameters_v2(value['playback'])
    lessons=value['lessons']
    if type(lessons) is not list or not 1<=len(lessons)<=50:fail('lessons','expected 1..50')
    by_id={}
    for lesson in lessons:
        obj(lesson,('lesson_id','source','two_targets','four_targets'),'lesson')
        lid=lesson['lesson_id'];text(lid,128,'lesson_id',identifier=True)
        if lid in by_id:fail('lesson_id','duplicate')
        by_id[lid]=lesson
        src=lesson['source'];obj(src,('book_id','unit','number','revision'),'source');text(src['book_id'],128,'book_id',identifier=True)
        for k in ('unit','number','revision'):integer(src[k],1,2147483647,'source.'+k)
        for key in ('two_targets','four_targets'):
            arr=lesson[key]
            if type(arr) is not list or len(arr)>200:fail(key,'invalid targets')
            seen=set()
            for c in arr:
                text(c,1,key)
                if not '\u3400'<=c<='\u9fff' or c in seen:fail(key,'expected unique Han targets')
                seen.add(c)
    assets=value['audio_assets']
    if type(assets) is not list or len(assets)>20000:fail('audio_assets','invalid resources')
    asset_ids=set()
    for asset in assets:
        obj(asset,('asset_id','sha256','media_type'),'audio_asset');aid=asset['asset_id'];text(aid,128,'asset_id',identifier=True)
        if aid in asset_ids:fail('asset_id','duplicate')
        asset_ids.add(aid)
        if type(asset['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',asset['sha256']):fail('audio_asset.sha256','invalid hash')
        if asset['media_type'] not in ('audio/pcm',):fail('media_type','unsupported audio')
    phases=value['phases']
    if type(phases) is not list or not 1<=len(phases)<=2:fail('phases','expected 1..2 enabled phases')
    phase_ids=set();types=set();item_ids=set();total=0
    for phase in phases:
        obj(phase,('phase_id','type','items'),'phase');pid=phase['phase_id'];text(pid,128,'phase_id',identifier=True)
        kind=phase['type']
        if kind not in ('recognition','dictation') or kind in types or pid in phase_ids:fail('phase','duplicate or unsupported phase')
        types.add(kind);phase_ids.add(pid)
        items=phase['items']
        if type(items) is not list or len(items)>10000:fail('phase.items','invalid items')
        coverage={lid:set() for lid in by_id}
        for item in items:
            obj(item,('item_id','lesson_id','text','covers','audio_asset_id'),'item')
            iid=item['item_id'];text(iid,128,'item_id',identifier=True)
            if iid in item_ids:fail('item_id','duplicate')
            item_ids.add(iid);lid=item['lesson_id']
            if type(lid) is not str or lid not in by_id:fail('item.lesson_id','unknown lesson')
            word=item['text'];text(word,32,'item.text')
            targets=by_id[lid]['two_targets' if kind=='recognition' else 'four_targets'];covers=item['covers']
            if type(covers) is not list or not covers or any(type(c) is not str or c not in targets or c not in word for c in covers) or len(set(covers))!=len(covers):fail('item.covers','invalid coverage')
            if kind=='recognition':
                if item['audio_asset_id'] is not None:fail('item.audio_asset_id','recognition is display only')
            else:
                text(item['audio_asset_id'],128,'item.audio_asset_id',identifier=True)
                if item['audio_asset_id'] not in asset_ids:fail('item.audio_asset_id','missing resource')
            coverage[lid].update(covers);total+=1
        for lid,lesson in by_id.items():
            if coverage[lid]!=set(lesson['two_targets' if kind=='recognition' else 'four_targets']):fail('phase.items','incomplete target coverage')
    if not total:fail('phases','no executable items')
    digest=value['content_hash']
    if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest) or digest!=content_hash(value):fail('content_hash','snapshot mismatch')
    if len(canonical_json(value).encode('utf-8'))>MAX_TASK_BYTES:fail('task','payload too large')
    return copy.deepcopy(value)
