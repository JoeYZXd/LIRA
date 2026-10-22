import json

import pytest

from lira.tv_voice import TvVoiceTools
from test_tv_voice import Sender, answer


@pytest.fixture
def books(tmp_path):
    (tmp_path/'chinese.json').write_text(json.dumps({
        'id':'chinese-1','title':'测试语文一年级','lessons':[
            {'number':1,'unit':1,'title':'第一课','four':['日','月'],'two':['山']},
            {'number':2,'unit':1,'title':'第二课','four':['水'],'two':['火']},
            {'number':3,'unit':2,'title':'第三课','four':['田'],'two':['土']},
        ]},ensure_ascii=False),encoding='utf-8')
    return tmp_path


@pytest.mark.asyncio
async def test_missing_book_is_honest_and_never_prepares_display():
    tv=TvVoiceTools(sender=Sender())
    result=await tv.call('prepare_pinyin_practice',{'scope_type':'lesson','start':3})
    assert result['status']=='textbook_required' and tv.pending is None


@pytest.mark.asyncio
async def test_range_and_category_are_collected_before_consent(books):
    tv=TvVoiceTools(sender=Sender(),textbook_dir=books)
    result=await tv.call('prepare_pinyin_practice',{})
    assert result['status']=='needs_input'
    assert 'scope_type' in result['missing_parameters']
    result=await tv.call('prepare_pinyin_practice',{'scope_type':'lesson_range','start':1})
    assert set(result['missing_parameters'])=={'end','character_group'}
    assert tv.pending is None


@pytest.mark.asyncio
@pytest.mark.parametrize('scope,start,end,group,expected',[
    ('lesson',1,None,'four','日　月'),
    ('unit',1,None,'two','山　火'),
    ('lesson_range',2,3,'all','水　火　田　土'),
])
async def test_textbook_selection_uses_generic_display_and_consent(books,scope,start,end,group,expected):
    tv=TvVoiceTools(sender=Sender(),textbook_dir=books)
    tv.event('user_text',{'four':'四会字','two':'二会字','all':'两类字都练'}[group])
    args={'scope_type':scope,'start':start,'character_group':group}
    if end is not None:args['end']=end
    result=await tv.call('prepare_pinyin_practice',args)
    assert result['status']=='awaiting_consent' and not tv.sender.sent
    assert tv.pending['payload']['text']==expected
    assert '测试语文一年级' in result['question']
    answer(tv,result)
    await tv.call('confirm_tv_display',{'request_id':result['request_id']});await tv.task
    assert tv.sender.sent[0]['text']==expected


@pytest.mark.asyncio
@pytest.mark.parametrize('args',[
    {'scope_type':'lesson','start':99,'character_group':'four'},
    {'scope_type':'lesson_range','start':3,'end':1,'character_group':'four'},
    {'book_id':'unknown','scope_type':'lesson','start':1,'character_group':'four'},
])
async def test_invalid_selection_does_not_guess(books,args):
    tv=TvVoiceTools(sender=Sender(),textbook_dir=books)
    result=await tv.call('prepare_pinyin_practice',args)
    assert result['status']=='needs_input' and tv.pending is None


@pytest.mark.asyncio
async def test_model_cannot_guess_category_or_override_observed_choice(books):
    tv=TvVoiceTools(sender=Sender(),textbook_dir=books)
    args={'scope_type':'lesson','start':1,'character_group':'four'}
    assert (await tv.call('prepare_pinyin_practice',args))['status']=='needs_input'
    tv.event('user_text','两类字都练')
    result=await tv.call('prepare_pinyin_practice',args)
    assert result['status']=='needs_input' and result['observed_character_group']=='all'
    assert tv.pending is None
    args['character_group']='all'
    assert (await tv.call('prepare_pinyin_practice',args))['status']=='awaiting_consent'
    await tv.close()
    assert tv.practice_group is None


@pytest.mark.parametrize('mutation',[
    lambda b:b['lessons'][0].update(four=['ri']),
    lambda b:b['lessons'][0].pop('two'),
    lambda b:b['lessons'][0].update(number=True),
    lambda b:b['lessons'][1].update(number=1),
])
def test_invalid_imports_never_supply_practice_words(books,mutation):
    from lira.textbook_practice import TextbookPractice
    path=books/'chinese.json';book=json.loads(path.read_text(encoding='utf-8'));mutation(book)
    path.write_text(json.dumps(book),encoding='utf-8')
    assert TextbookPractice(books).books()==[]


@pytest.mark.parametrize('text,expected',[
    ('四会字和二会字都练','all'),('四会和二会','all'),('都练','all'),
    ('四会字还是二会字',None),('不是四会字',None),
    ('四会字和二会字有什么区别？',None),('四会字是什么意思？',None),
])
def test_spoken_category_choice_and_ambiguity(text,expected):
    tv=TvVoiceTools();tv.practice_group='four'
    tv.event('user_text',text)
    assert tv.practice_group==expected


def test_duplicate_printed_numbers_require_unit(books):
    from lira.textbook_practice import TextbookPractice
    path=books/'chinese.json';book=json.loads(path.read_text(encoding='utf-8'))
    book['lessons'].append({'number':1,'unit':2,'title':'识字第一课','four':['天'],'two':['地']})
    path.write_text(json.dumps(book),encoding='utf-8')
    practice=TextbookPractice(books)
    args={'scope_type':'lesson','start':1,'character_group':'four'}
    assert practice.prepare(args)['missing_parameters']==['unit']
    args['unit']=2
    assert practice.prepare(args)['text']=='天'
