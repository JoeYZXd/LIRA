import pytest
from lira.audio.voice_metrics import VoiceMetrics


def test_metrics_are_bounded_numeric_and_disabled_by_default():
    metrics = VoiceMetrics(capacity=2)
    metrics.trace(turn_id=1).mark('asr_submit')
    assert metrics.snapshot() == []
    metrics = VoiceMetrics(enabled=True, capacity=2, clock=lambda: 12.0)
    trace = metrics.trace(turn_id=1, owner='owner-1')
    for _ in range(3):
        trace.mark('pcm_ready', frames=12)
    assert len(metrics.snapshot()) == 2
    assert metrics.snapshot()[0]['turn_id'] == 1
    with pytest.raises(ValueError):
        trace.mark('pcm_ready', text='secret')


def test_late_events_keep_identity_and_privacy_epoch():
    metrics = VoiceMetrics(enabled=True)
    old = metrics.trace(turn_id=1, owner='old')
    new = metrics.trace(turn_id=2, owner='new')
    new.mark('output_consumed')
    old.mark('synthesis_end')
    assert [row['owner'] for row in metrics.snapshot()] == ['new', 'old']
    metrics.apply_privacy(True)
    metrics.apply_privacy(False)
    old.mark('output_drained')
    assert metrics.snapshot() == []


def test_missing_times_are_none_and_chime_is_not_answer():
    metrics = VoiceMetrics(enabled=True, clock=lambda: 10.0)
    trace = metrics.trace(turn_id=3)
    trace.mark('asr_submit', at=1.0)
    trace.mark('output_consumed', at=2.0, answer=0)
    assert metrics.summary()[0]['asr_to_output_seconds'] is None
    trace.mark('output_consumed', at=4.0, answer=1)
    trace.mark('synthesis_start', at=1.5, segment=0)
    trace.mark('synthesis_end', at=3.5, segment=0, audio_seconds=4.0)
    row = metrics.summary()[0]
    assert row['asr_to_output_seconds'] == 3.0
    assert row['synthesis_seconds'] == 2.0
    assert row['rtf'] == 0.5


def test_runtime_binding_joins_submission_before_turn_and_keeps_owner():
    metrics = VoiceMetrics(enabled=True)
    submitted = metrics.trace(utterance_id=7)
    submitted.mark('asr_submit', at=1)
    model = submitted.bind(turn_id=4)
    model.mark('llm_request', at=2)
    model.mark('llm_content', at=3)
    output = submitted.bind(turn_id=4, owner='operation-8')
    output.mark('output_consumed', at=5, answer=1)
    assert len(metrics.summary()) == 1
    assert metrics.summary()[0]['asr_to_output_seconds'] == 4
    assert metrics.snapshot()[-1]['owner'] == 'operation-8'


def test_role_only_content_and_broken_observer_do_not_affect_llm():
    from lira.audio.voice_metrics import current_trace
    from lira.llm.client import _ModelObserve
    metrics = VoiceMetrics(enabled=True)
    token = current_trace.set(metrics.trace(utterance_id=9))
    try:
        observe = _ModelObserve(None, turn_id=3, path='chat')
        observe.request(2)
        observe.delta('')
        observe.delta('  ')
        assert [r['event'] for r in metrics.snapshot()] == ['llm_request']
        observe.delta('answer')
        observe.delta('tail')
        assert [r['event'] for r in metrics.snapshot()] == ['llm_request', 'llm_content']
    finally:
        current_trace.reset(token)


def test_measured_synthesis_records_full_compute_not_remaining_prefetch_wait(monkeypatch):
    import numpy as np
    from lira.audio.tts import TtsEngine, _PlaybackRequest, PlaybackCompletion
    import lira.audio.tts as tts
    times = iter([10.0, 12.0])
    monkeypatch.setattr(tts.time, 'monotonic', lambda: next(times))
    metrics = VoiceMetrics(enabled=True, clock=lambda: 12.0)
    engine = TtsEngine.__new__(TtsEngine)
    engine.sample_rate = 4
    engine.generate = lambda text, speed: np.ones(16)
    request = _PlaybackRequest(PlaybackCompletion('owner'), 'test', 1, 1,
                               trace=metrics.trace(turn_id=1, owner='owner'))
    engine._generate_measured(request, 'test')
    assert metrics.summary()[0]['synthesis_seconds'] == 2.0
    assert metrics.summary()[0]['rtf'] == 0.5
