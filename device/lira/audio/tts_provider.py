"""Optional live-answer provider configuration; finite speech always stays local."""
from dataclasses import dataclass, field
from typing import Protocol
from collections.abc import AsyncIterable

@dataclass(frozen=True)
class OnlineTtsConfig:
    enabled: bool = False
    resource_id: str = 'seed-tts-2.0'
    speaker: str = ''
    timeout_seconds: float = 10.0
    api_key: str = field(default='', repr=False)

    @property
    def ready(self):
        return self.enabled and bool(self.api_key.strip() and self.resource_id.strip() and self.speaker.strip())

class TtsProvider(Protocol):
    def stream(self, text: AsyncIterable[str], *, sample_rate: int, speed: float,
               allowed): ...
