import asyncio
from dataclasses import dataclass
import io
import json
import threading
from types import SimpleNamespace

from PIL import Image
import numpy as np
import pytest

from lira.textbook_import import TextbookImportStore
from lira import textbook_recognizer as rec


@dataclass
class Config:
    web_search_enabled: bool = True


def setup(tmp_path, monkeypatch, responses, text='第一单元第一课小蝌蚪找妈妈会写字两哪里宽顶肚皮孩跳会认字塘脑袋灰捕迎阿姨'):
    raw = io.BytesIO()
    Image.new('RGB', (20, 20), 'red').save(raw, 'PNG')
    store = TextbookImportStore(tmp_path / 'drafts', tmp_path / 'books')
    draft = store.create([('page.png', raw.getvalue())])
    store.set_recognizing(draft['id'])
    store.update_page_text(draft['id'], 1, text)
    calls = []
    class Client:
        def __init__(self, cfg, **kwargs):
            assert cfg.web_search_enabled is False
            self._client = self
        async def close(self):
            calls.append('closed')
        async def chat(self, prompt, **kwargs):
            calls.append((json.loads(prompt), kwargs))
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return json.dumps(value, ensure_ascii=False)
    monkeypatch.setattr(rec, 'LlmClient', Client)
    runtime = SimpleNamespace(privacy=SimpleNamespace(is_on=False), _cfg=SimpleNamespace(llm=Config()))
    return store, draft['id'], runtime, calls


def lesson(**kwargs):
    return dict(unit=1, number=1, title='小蝌蚪找妈妈', four=['两'], two=['塘'], source_pages=[1], **kwargs)


@pytest.mark.asyncio
async def test_native_text_isolated_client_review_only(tmp_path, monkeypatch):
    store, id, runtime, calls = setup(tmp_path, monkeypatch, [dict(title='语文二上', lessons=[lesson()])])
    await rec.recognize_textbook(store, id, runtime, asyncio.Lock())
    draft = store.get(id)
    assert draft['status'] == 'review'
    assert draft['lessons'][0]['reviewed'] is False
    assert draft['lessons'][0]['four'] == ['两']
    assert list(store.books_dir.iterdir()) == []
    assert calls[-1] == 'closed'
    assert calls[0][1]['voice_log'] is False
    assert calls[0][1]['on_wait_feedback'] is None
    assert calls[0][1]['timeout_seconds'] == 180


@pytest.mark.asyncio
async def test_ocr_bgr_and_mutex(tmp_path, monkeypatch):
    store, id, runtime, calls = setup(tmp_path, monkeypatch, [dict(lessons=[lesson()])], text='')
    mutex = asyncio.Lock()
    class Ocr:
        def detect(self, image):
            assert mutex.locked()
            assert image[0, 0].tolist() == [0, 0, 255]
            return [[[0, 0], [10, 0], [10, 10], [0, 10]]]
        def recognize(self, crop):
            assert mutex.locked()
            return '会写字两，会认字塘'
    runtime.ocr = Ocr()
    await rec.recognize_textbook(store, id, runtime, mutex)
    assert store.get(id)['pages'][0]['text'] == '会写字两，会认字塘'
    assert not mutex.locked()


@pytest.mark.asyncio
async def test_privacy_and_sanitized_failure(tmp_path, monkeypatch):
    store, id, runtime, calls = setup(tmp_path, monkeypatch, [RuntimeError('secret-key')])
    await rec.recognize_textbook(store, id, runtime, asyncio.Lock())
    assert store.get(id)['status'] == 'failed'
    assert 'secret' not in store.get(id)['error']
    assert store.get(id)['pages'][0]['text']
    store.set_recognizing(id)
    runtime.privacy.is_on = True
    calls.clear()
    await rec.recognize_textbook(store, id, runtime, asyncio.Lock())
    assert calls == []
    assert '隐私' in store.get(id)['error']


def test_chunk_bound_and_actual_page_numbers():
    chunks = list(rec._chunks([{'number': 4, 'text': '文' * 30000}, {'number': 8, 'text': '字' * 19000}]))
    assert all(sum(len(p['text']) for p in chunk) <= 800 for chunk in chunks)
    assert {p['number'] for chunk in chunks for p in chunk} == {4, 8}
    assert sum(len(p['text']) for chunk in chunks for p in chunk) == 49000


def test_merge_dedup_conflict_and_source_filter():
    result = dict(title='', lessons=[], warnings=[])
    pages = [dict(number=1, text='两塘字')]
    first = lesson()
    rec._merge(result, dict(lessons=[first, first]), pages)
    assert len(result['lessons']) == 1
    assert result['lessons'][0]['four'] == ['两']
    second = dict(first, title='别的课', four=['两', '龙', 'ab'])
    rec._merge(result, dict(lessons=[second]), pages)
    assert all(r['number'] is None for r in result['lessons'])
    assert result['lessons'][1]['four'] == ['两']
    assert result['warnings']


def test_duplicate_numbers_across_units_valid_unknown_and_garden():
    result = dict(title='', lessons=[], warnings=[])
    rec._merge(result, dict(lessons=[lesson(), dict(lesson(), unit=2),
        dict(lesson(), unit='二', number='1', title='未知'),
        dict(lesson(), title='语文园地一')]), [dict(number=1, text='两塘')])
    assert [r['unit'] for r in result['lessons']] == [1, 2, None]
    assert result['lessons'][-1]['number'] is None


@pytest.mark.asyncio
async def test_empty_ocr_never_succeeds(tmp_path, monkeypatch):
    store, id, runtime, calls = setup(tmp_path, monkeypatch, [], text='')
    monkeypatch.setattr(rec, '_ocr_page', lambda *_: '')
    runtime.ocr = object()
    await rec.recognize_textbook(store, id, runtime, asyncio.Lock())
    assert store.get(id)['status'] == 'failed'
    assert calls == []


@pytest.mark.asyncio
async def test_cancel_keeps_npu_locked_until_worker_finishes(tmp_path, monkeypatch):
    store, id, runtime, calls = setup(tmp_path, monkeypatch, [], text='')
    started, release = threading.Event(), threading.Event()
    def worker(*_):
        started.set()
        release.wait(5)
        return '文字'
    monkeypatch.setattr(rec, '_ocr_page', worker)
    runtime.ocr = object()
    mutex = asyncio.Lock()
    task = asyncio.create_task(rec.recognize_textbook(store, id, runtime, mutex))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert mutex.locked()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not mutex.locked()
    assert store.get(id)['status'] == 'failed'
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_store_io_never_runs_on_voice_event_loop(tmp_path, monkeypatch, fail):
    response = RuntimeError('network failure') if fail else dict(lessons=[lesson()])
    store, id, runtime, calls = setup(tmp_path, monkeypatch, [response], text='')
    runtime.ocr = object()
    monkeypatch.setattr(rec, '_ocr_page', lambda *_: '会写字两，会认字塘')
    event_thread = threading.get_ident()
    observed = []
    for name in ('get', 'page_path', 'update_page_text', 'finish_recognition', 'fail_recognition'):
        original = getattr(store, name)
        def checked(*args, _name=name, _original=original):
            assert threading.get_ident() != event_thread, _name
            observed.append(_name)
            return _original(*args)
        monkeypatch.setattr(store, name, checked)
    await rec.recognize_textbook(store, id, runtime, asyncio.Lock())
    assert {'get', 'page_path', 'update_page_text'} <= set(observed)
    assert ('fail_recognition' if fail else 'finish_recognition') in observed


def box(x0, y0, x1, y1):
    return np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])


def test_dense_tiles_restore_global_coordinates_join_seams_and_deduplicate(tmp_path):
    frame = np.zeros((1200, 1200, 3), dtype=np.uint8)
    frame[:, :, 0] = np.arange(1200, dtype=np.uint16)[None, :] // 5
    frame[:, :, 1] = np.arange(1200, dtype=np.uint16)[:, None] // 5
    frame[:, :, 2] = 7
    path = tmp_path / 'page.png'
    Image.fromarray(frame).save(path)
    calls, crops = [], []
    class Ocr:
        def detect(self, image):
            h, w = image.shape[:2]
            calls.append((h, w))
            if h > 640 or w > 640:
                return []  # Whole-page scaling missed the small print.
            x, y = int(image[0, 0, 2])*5, int(image[0, 0, 1])*5
            result = []
            for line_y in (500, 560):
                x0, x1 = max(450, x), min(850, x+w)
                y0, y1 = max(line_y, y), min(line_y+20, y+h)
                if x0 < x1 and y0 < y1:
                    result.append(box(x0-x, y0-y, x1-x, y1-y))
            return result
        def recognize(self, image):
            crops.append(image)
            return '完整字行'
    text = rec._ocr_page(Ocr(), path)
    assert text == '完整字行\n完整字行'
    assert len(calls) <= 17
    assert all(image.shape[1] == 400 for image in crops)
    assert [int(image[0, 0, 1])*5 for image in crops] == [500, 560]


def test_two_columns_keep_numbers_beside_titles_and_header_first():
    header = box(10, 0, 990, 20)
    left, right = [], []
    for y in (100, 200, 300, 400):
        left.extend([box(10, y, 30, y+20), box(60, y, 450, y+20)])
        right.extend([box(530, y, 550, y+20), box(580, y, 980, y+20)])
    shuffled = right[::-1] + [header] + left[::-1]
    ordered = rec._textbook_order(shuffled, 1000)
    expected = [header] + left + right
    assert len(ordered) == len(expected)
    assert all(np.array_equal(a, b) for a, b in zip(ordered, expected))


def test_interior_spanning_heading_uses_general_order():
    boxes = [box(10, y, 450, y+20) for y in (100, 200, 300, 400)]
    boxes += [box(550, y, 990, y+20) for y in (100, 200, 300, 400)]
    boxes.append(box(10, 240, 990, 260))
    assert all(np.array_equal(a, b) for a, b in zip(
        rec._textbook_order(boxes, 1000), rec.sort_reading_order(boxes)))
