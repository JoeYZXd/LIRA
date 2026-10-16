import json

from lira.llm.timing_log import ModelTiming, TimingLog


def test_durable_timing_does_not_store_content_and_survives_new_session(tmp_path):
    path = tmp_path / 'timing' / 'interaction.jsonl'
    log = TimingLog(path)
    timing = ModelTiming(log, path='search', voice=True, model='test', turn=2, trace=None)
    timing.delta('SECRET dialogue')
    timing.delta('more dialogue')
    timing.finish('ok')
    log._queue.join()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r['event'] for r in rows] == ['model_start', 'model_first_content', 'model_end']
    assert len({r['request'] for r in rows}) == 1
    assert rows[-1]['chunks'] == 2 and rows[-1]['output_chars'] == 28
    assert rows[-1]['elapsed'] >= rows[1]['elapsed'] >= 0
    assert 'SECRET' not in path.read_text() and 'dialogue' not in path.read_text()
    second = TimingLog(path)
    second.emit('test')
    second._queue.join()
    assert len(path.read_text().splitlines()) == 4
    assert second.session != log.session


def test_privacy_invalidates_inflight_measurement(tmp_path):
    log = TimingLog(tmp_path / 'interaction.jsonl')
    timing = ModelTiming(log, path='stream', voice=False, model='test', turn=None, trace=None)
    log._queue.join()
    log.apply_privacy(True)
    timing.delta('private')
    log.emit('private')
    log.apply_privacy(False)
    timing.finish('ok')
    log.emit('after_privacy')
    log._queue.join()
    rows = [json.loads(line) for line in (tmp_path / 'interaction.jsonl').read_text().splitlines()]
    assert [r['event'] for r in rows] == ['model_start', 'after_privacy']


def test_rotation_is_bounded(tmp_path):
    log = TimingLog(tmp_path / 'interaction.jsonl')
    log._handler.maxBytes = 250
    for _ in range(30):
        log.emit('test')
    log._queue.join()
    assert len(list(tmp_path.glob('interaction.jsonl*'))) <= 5
