"""Small cleaned-audio activity detector; no audio persistence or text logging."""
from __future__ import annotations
import numpy as np


class SpeechActivity:
    def __init__(self, *, sample_rate=16000, onset_seconds=0.08, silence_seconds=0.65):
        self.sample_rate = sample_rate
        self.onset_samples = int(sample_rate * onset_seconds)
        self.silence_samples = int(sample_rate * silence_seconds)
        self.reset()

    def reset(self):
        self.active = False
        self._voiced = self._quiet = 0
        self._noise = 0.002

    def feed(self, samples):
        events = []
        # Fixed 20ms windows make detection independent of callback chunk size.
        for offset in range(0, len(samples), 320):
            frame = samples[offset:offset + 320]
            if not len(frame):
                continue
            rms = float(np.sqrt(np.mean(frame * frame)))
            voiced = rms > max(0.008, min(self._noise * 3.5, 0.035))
            if voiced:
                self._voiced += len(frame)
                self._quiet = 0
                if not self.active and self._voiced >= self.onset_samples:
                    self.active = True
                    events.append("started")
            else:
                self._voiced = 0
                self._quiet += len(frame)
                if not self.active:
                    self._noise = 0.98 * self._noise + 0.02 * rms
                if self.active and self._quiet >= self.silence_samples:
                    self.active = False
                    events.append("ended")
        return events
