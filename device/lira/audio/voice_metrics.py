"""Opt-in, bounded numeric voice measurements; never stores text, PCM or secrets.

Callbacks only append a fixed-size record. Export/summarize outside audio callbacks.
A trace captures identity and privacy epoch, so late work cannot refill cleared data.
"""
from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
import threading
import time

_FIELDS = frozenset({'segment', 'frames', 'audio_seconds', 'answer', 'buffer_frames',
                     'device_underflow', 'starvation_seconds', 'starvation_count'})
_EVENTS = frozenset({'asr_submit', 'llm_request', 'llm_content', 'segment_submit',
                     'synthesis_start', 'synthesis_end', 'pcm_ready', 'output_consumed',
                     'output_drained', 'cancel_requested', 'cancel_confirmed', 'buffer'})
current_trace: ContextVar[VoiceTrace | None] = ContextVar('voice_metrics_trace', default=None)
current_answer: ContextVar[bool] = ContextVar('voice_metrics_answer', default=False)


@dataclass(frozen=True)
class VoiceTrace:
    metrics: VoiceMetrics
    epoch: int
    utterance_id: int | None = None
    turn_id: int | None = None
    owner: str | None = None

    def bind(self, *, turn_id=None, owner=None):
        return VoiceTrace(self.metrics, self.epoch, self.utterance_id,
                          self.turn_id if turn_id is None else turn_id,
                          self.owner if owner is None else owner)

    def mark(self, event, *, at=None, **values):
        if event not in _EVENTS or not values.keys() <= _FIELDS:
            raise ValueError('voice metrics accepts only known numeric fields')
        if any(not isinstance(value, (int, float)) for value in values.values()):
            raise ValueError('voice metrics values must be numeric')
        self.metrics._record(self, event, at, values)


class VoiceMetrics:
    def __init__(self, *, enabled=False, capacity=2048, clock=time.monotonic, sink=None):
        self.enabled = enabled
        self._sink = sink
        self._events = deque(maxlen=max(1, capacity))
        self._clock = clock
        self._epoch = 0
        self._privacy = False
        self.dropped_events = 0
        self._lock = threading.Lock()

    def trace(self, *, utterance_id=None, turn_id=None, owner=None):
        return VoiceTrace(self, self._epoch, utterance_id, turn_id, owner)

    def _record(self, trace, event, at, values):
        if not self.enabled or self._privacy:
            return
        # Audio callbacks never wait for an exporter holding the lock.
        if not self._lock.acquire(blocking=False):
            self.dropped_events += 1
            return
        try:
            if self._privacy or trace.epoch != self._epoch:
                return
            row = dict(stage=event, at=self._clock() if at is None else at,
                                     utterance_id=trace.utterance_id, turn_id=trace.turn_id,
                                     owner=trace.owner, **values)
            self._events.append(dict(row, event=event))
            if self._sink is not None:
                try:
                    self._sink(row)
                except Exception:
                    pass
        finally:
            self._lock.release()

    def apply_privacy(self, enabled):
        with self._lock:
            self._privacy = enabled
            if enabled:
                self._epoch += 1
                self._events.clear()

    def snapshot(self):
        with self._lock:
            return list(self._events)

    def summary(self):
        groups = {}
        for row in self.snapshot():
            key = (row['utterance_id'], None if row['utterance_id'] is not None else row['turn_id'])
            groups.setdefault(key, []).append(row)
        summaries = []
        for (utterance, turn), rows in groups.items():
            def first(event, answer=False):
                return next((r['at'] for r in rows if r['event'] == event
                             and (not answer or r.get('answer') == 1)), None)
            def elapsed(a, b):
                return None if a is None or b is None else b - a
            starts = {(r['owner'], r.get('segment')): r['at'] for r in rows
                      if r['event'] == 'synthesis_start'}
            durations = [(r['at'] - starts[(r['owner'], r.get('segment'))], r.get('audio_seconds', 0))
                         for r in rows if r['event'] == 'synthesis_end'
                         and (r['owner'], r.get('segment')) in starts]
            synth = sum(d[0] for d in durations) if durations else None
            audio = sum(d[1] for d in durations)
            turn = next((r["turn_id"] for r in rows if r["turn_id"] is not None), turn)
            summaries.append(dict(utterance_id=utterance, turn_id=turn,
                asr_to_output_seconds=elapsed(first('asr_submit'), first('output_consumed', True)),
                llm_first_content_seconds=elapsed(first('llm_request'), first('llm_content')),
                synthesis_seconds=synth, rtf=synth / audio if audio and synth is not None else None))
        return summaries


def mark(trace, event, **values):
    """Observation is optional and exceptions must not alter speech behavior."""
    if trace is not None:
        try:
            trace.mark(event, **values)
        except Exception:
            pass
