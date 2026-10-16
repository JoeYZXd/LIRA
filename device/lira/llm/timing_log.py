"""Bounded, metadata-only timing journal. Disk I/O never runs on audio threads."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from logging.handlers import RotatingFileHandler
import logging
from queue import Queue, Full
import threading
import time
import uuid


class TimingLog:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(path, maxBytes=4*1024*1024,
                                           backupCount=4, encoding='utf-8')
        self._handler.setFormatter(logging.Formatter('%(message)s'))
        self._queue = Queue(maxsize=4096)
        self.session = uuid.uuid4().hex
        self.epoch = 0
        self.private = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._write, daemon=True, name='llm-timing-log')
        self._thread.start()

    def apply_privacy(self, enabled):
        self.private = enabled
        self.epoch += 1

    def emit(self, event, **fields):
        if self.private:
            return
        row = dict(version=1, session=self.session, event=event,
                   utc=datetime.now(timezone.utc).isoformat(), mono=time.monotonic(),
                   dropped_events=self.dropped, **fields)
        try:
            self._queue.put_nowait((self.epoch, row))
        except Full:
            self.dropped += 1

    def voice(self, row):
        self.emit('voice', **row)

    def close(self):
        self._queue.put(None, timeout=2)
        self._thread.join(timeout=2)

    def _write(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    self._handler.close()
                    return
                epoch, row = item
                if epoch == self.epoch and not self.private:
                    self._handler.emit(logging.makeLogRecord(
                        {'msg': json.dumps(row, separators=(',', ':'))}))
            except Exception:
                self.dropped += 1
            finally:
                self._queue.task_done()


class ModelTiming:
    def __init__(self, log, *, path, voice, model, turn, trace):
        self.log = log
        self.epoch = log.epoch if log else None
        self.started = time.monotonic()
        self.last = None
        self.max_gap = 0.0
        self.chunks = self.chars = 0
        self.fields = dict(request=uuid.uuid4().hex, path=path, voice=voice,
                           model=model, turn_id=turn,
                           utterance_id=trace.utterance_id if trace else None)
        self.emit('model_start')

    def emit(self, event, **values):
        if self.log and self.epoch == self.log.epoch:
            self.log.emit(event, elapsed=time.monotonic()-self.started,
                          **self.fields, **values)

    def delta(self, content):
        if not content or not content.strip():
            return
        now = time.monotonic()
        if self.last is None:
            self.emit('model_first_content')
        else:
            self.max_gap = max(self.max_gap, now-self.last)
        self.last = now
        self.chunks += 1
        self.chars += len(content)

    def finish(self, status):
        self.emit('model_end', status=status, chunks=self.chunks,
                  output_chars=self.chars, max_content_gap=self.max_gap)
