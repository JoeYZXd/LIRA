import io

import pytest
from PIL import Image

from lira.textbook_import import TextbookImportStore
from lira.textbook_practice import TextbookPractice


def picture():
    stream = io.BytesIO()
    Image.new('RGB', (20, 30), 'white').save(stream, 'PNG')
    return stream.getvalue()


@pytest.fixture
def store(tmp_path):
    return TextbookImportStore(tmp_path / 'drafts', tmp_path / 'books')


def lesson(**changes):
    return dict(number=1, unit=1, title='小蝌蚪找妈妈', four=['两'], two=['塘'],
                source_pages=[1], reviewed=True, **changes)


def test_review_publish_and_voice_contract(store):
    draft = store.create([('page.png', picture())])
    assert draft['status'] == 'uploaded'
    assert store.page_path(draft['id'], 1).is_file()
    assert TextbookPractice(store.books_dir).books() == []
    draft = store.set_recognizing(draft['id'])
    draft = store.finish_recognition(draft['id'], {'title': '二年级上册', 'lessons': [lesson()]})
    assert draft['lessons'][0]['reviewed'] is False
    with pytest.raises(ValueError):
        store.publish(draft['id'], draft['revision'], True)
    draft['lessons'][0]['reviewed'] = True
    draft = store.update(draft['id'], draft)
    book = store.publish(draft['id'], draft['revision'], True)
    assert TextbookPractice(store.books_dir).books() == [book]
    assert store.publish(draft['id'], draft['revision'], True) == book


def test_revision_conflict_and_no_overwrite(store):
    original = store.create([('page.png', picture())])
    data = dict(original, title='课本', lessons=[lesson()])
    draft = store.update(original['id'], data)
    with pytest.raises(ValueError):
        store.update(original['id'], data)
    target = store.books_dir / (draft['id'] + '.json')
    target.write_text('{}')
    with pytest.raises(ValueError):
        store.publish(draft['id'], draft['revision'], True)
    assert target.read_text() == '{}'


@pytest.mark.parametrize('change', [{'number': None}, {'unit': None}, {'four': ['hello']},
                                   {'source_pages': [2]}, {'reviewed': False}])
def test_invalid_or_unreviewed_never_publishes(store, change):
    draft = store.create([('page.png', picture())])
    row = lesson()
    row.update(change)
    with pytest.raises(ValueError):
        draft = store.update(draft['id'], dict(draft, title='教材', lessons=[row]))
        store.publish(draft['id'], draft['revision'], True)
    assert list(store.books_dir.glob('*.json')) == []


def test_bad_upload_cleanup_and_paths(store):
    with pytest.raises(ValueError):
        store.create([('evil.png', b'not an image')])
    assert store.list_drafts() == []
    for bad in ['../books', '/tmp', '..', 'x' * 200]:
        with pytest.raises(ValueError):
            store.get(bad)
    draft = store.create([('../../ignore.png', picture())])
    with pytest.raises(ValueError):
        store.page_path(draft['id'], 0)


def test_failed_recognition_retains_reviewable_pages(store):
    draft = store.create([('page.png', picture())])
    store.set_recognizing(draft['id'])
    store.update_page_text(draft['id'], 1, '第一单元')
    draft = store.fail_recognition(draft['id'], '无法识别')
    assert draft['status'] == 'failed'
    assert store.page_path(draft['id'], 1).exists()
    assert draft['error'] == '无法识别'
    assert draft['pages'][0]['text'] == '第一单元'


def test_publish_requires_explicit_confirmation(store):
    draft = store.create([('page.png', picture())])
    draft = store.update(draft['id'], dict(draft, title='教材', lessons=[lesson()]))
    for confirmed in [False, 'true', 1, None]:
        with pytest.raises(ValueError):
            store.publish(draft['id'], draft['revision'], confirmed)


def test_duplicate_lessons_rejected(store):
    draft = store.create([('page.png', picture())])
    with pytest.raises(ValueError):
        store.update(draft['id'], dict(draft, title='教材', lessons=[lesson(), lesson()]))


def test_same_printed_number_in_different_units_publishes(store):
    draft = store.create([('page.png', picture())])
    literacy = lesson()
    literacy.update(unit=2, title='场景歌', four=['园'])
    draft = store.update(draft['id'], dict(draft, title='教材', lessons=[lesson(), literacy]))
    book = store.publish(draft['id'], draft['revision'], True)
    assert [(row['unit'], row['number']) for row in book['lessons']] == [(1, 1), (2, 1)]


def test_recognition_cannot_replace_preview_url(store):
    draft = store.create([('page.png', picture())])
    url = draft['pages'][0]['image_url']
    store.set_recognizing(draft['id'])
    draft = store.finish_recognition(draft['id'], dict(title='课本', lessons=[], pages=[
        dict(number=1, text='第一课', image_url='https://untrusted.invalid/image')]))
    assert draft['pages'][0]['text'] == '第一课'
    assert draft['pages'][0]['image_url'] == url


def test_incomplete_draft_editable_but_not_publishable(store):
    draft = store.create([('page.png', picture())])
    row = lesson()
    row['number'] = row['unit'] = None
    draft = store.update(draft['id'], dict(draft, title='待核对', lessons=[row]))
    assert draft['lessons'][0]['number'] is None
    with pytest.raises(ValueError):
        store.publish(draft['id'], draft['revision'], True)


def test_pdf_render_and_text(store):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument.new()
    page = pdf.new_page(200, 300)
    output = io.BytesIO()
    pdf.save(output)
    page.close()
    pdf.close()
    draft = store.create([('book.pdf', output.getvalue())])
    assert len(draft['pages']) == 1
    assert draft['pages'][0]['text'] == ''
    with Image.open(store.page_path(draft['id'], 1)) as rendered:
        assert max(rendered.size) <= 1600

def test_publish_recovers_after_draft_save_failure(store, monkeypatch):
    draft = store.create([('page.png', picture())])
    draft = store.update(draft['id'], dict(draft, title='课本', lessons=[lesson()]))
    save = store._save
    def fail(_):
        raise OSError('disk unavailable')
    monkeypatch.setattr(store, '_save', fail)
    with pytest.raises(OSError):
        store.publish(draft['id'], draft['revision'], True)
    assert store.get(draft['id'])['status'] == 'review'
    monkeypatch.setattr(store, '_save', save)
    book = store.publish(draft['id'], draft['revision'], True)
    assert store.get(draft['id'])['status'] == 'published'
    assert TextbookPractice(store.books_dir).books() == [book]


def test_cover_optional_lesson_material_and_revision_preserve_published_bytes(store):
    draft=store.create([('cover.png',picture())])
    row=lesson(expansions='两：两个、两边',text='小蝌蚪在池塘里。')
    draft=store.update(draft['id'],dict(draft,title='语文课本',lessons=[row],cover_page=1))
    assert store.list_drafts()[0]['cover_page']==1
    book=store.publish(draft['id'],draft['revision'],True)
    original=(store.books_dir/(book['id']+'.json')).read_bytes()
    assert book['lessons'][0]['expansions']=='两：两个、两边'
    assert book['lessons'][0]['text']=='小蝌蚪在池塘里。'
    assert store.publish(draft['id'],draft['revision'],True)==book
    view=store.read_book(book['id'])
    assert view['cover_page']==1 and len(view['pages'])==1
    revised=store.revise_book(book['id'])
    assert revised['id']!=book['id'] and revised['cover_page']==1
    assert revised['lessons'][0]['reviewed'] is False
    assert revised['lessons'][0]['text']==row['text']
    assert store.page_path(revised['id'],1).read_bytes()==picture()
    assert (store.books_dir/(book['id']+'.json')).read_bytes()==original


@pytest.mark.parametrize('field,value',[('expansions',['不接受列表']),('text',123),('text','字'*100001)],ids=['expansion-type','text-type','text-too-long'])
def test_optional_material_validation_does_not_erase_draft(store,field,value):
    draft=store.create([('page.png',picture())]);row=lesson();row[field]=value
    with pytest.raises(ValueError):
        store.update(draft['id'],dict(draft,title='语文',lessons=[row]))
    assert store.get(draft['id'])==draft


def test_legacy_published_book_without_cover_or_source_can_be_revised(store):
    import json
    book=dict(id='legacy-book',title='已有语文',lessons=[{k:v for k,v in lesson().items() if k not in ('reviewed','source_pages')}])
    (store.books_dir/'legacy.json').write_text(json.dumps(book),encoding='utf-8')
    view=store.read_book('legacy-book')
    assert view['cover_page'] is None and view['pages']==[]
    revised=store.revise_book('legacy-book')
    assert revised['lessons'][0]['text']==''
    assert revised['lessons'][0]['expansions']==''
    revised['lessons'][0]['reviewed']=True
    revised=store.update(revised['id'],revised)
    store.publish(revised['id'],revised['revision'],True)
    assert len(TextbookPractice(store.books_dir).books())==2


def test_page_units_lesson_links_and_published_cover_are_independent(store):
    draft=store.create([('one.png',picture()),('two.png',picture())])
    assert draft['cover_page']==1
    row=lesson();row['source_pages']=[2]
    draft=store.update(draft['id'],dict(draft,title='课本',lessons=[row],page_units=[{'number':2,'unit':1}]))
    assert draft['pages'][1]['unit']==1
    book=store.publish(draft['id'],draft['revision'],True)
    before=(store.books_dir/(book['id']+'.json')).read_bytes()
    view=store.read_book(book['id'])
    assert view['lessons'][0]['source_pages']==[2]
    changed=store.set_book_cover(book['id'],view['source_revision'],2)
    assert changed['cover_page']==2 and changed['source_revision']>view['source_revision']
    assert (store.books_dir/(book['id']+'.json')).read_bytes()==before
    with pytest.raises(ValueError):store.set_book_cover(book['id'],view['source_revision'],1)
    revised=store.revise_book(book['id'])
    assert revised['pages'][1]['unit']==1 and revised['lessons'][0]['source_pages']==[2]


@pytest.mark.parametrize('unit', [0,True,1000,'1'])
def test_invalid_page_association_does_not_write(store,unit):
    draft=store.create([('one.png',picture())])
    with pytest.raises(ValueError):store.update(draft['id'],dict(draft,page_units=[{'number':1,'unit':unit}]))
    assert store.get(draft['id'])==draft


def test_thumbnail_is_small_and_does_not_modify_original(store):
    raw=io.BytesIO();Image.new('RGB',(800,1200),'beige').save(raw,'PNG')
    draft=store.create([('page.png',raw.getvalue())]);source=store.page_path(draft['id'],1)
    before=source.read_bytes();preview=store.page_thumbnail(draft['id'],1)
    with Image.open(io.BytesIO(preview)) as image:
        assert image.format=='JPEG' and 0<image.width<=160 and 0<image.height<=220
    assert source.read_bytes()==before
    with pytest.raises(ValueError):store.page_thumbnail(draft['id'],2)
