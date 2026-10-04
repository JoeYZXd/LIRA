"""语音交互实时日志（dev 控制台展示面）：进程内环形事件区。

定位与纪律：
  - 这是**展示通道**，不是持久日志通道：条目可含识别文本/LLM 回复片段
    （dev 控制台显式能力，用户选择开启），全程内存、不落盘；各模块的
    ``logging`` 行保持"文本不落日志"纪律不变。
  - 单例 ``VOICE_LOG``（与 ``main.RING_LOG_HANDLER`` 同模式）：状态机/
    路由分发/LLM 客户端/装配层直接 import 记录，无需装配注入。
  - 环形上限自动挤出最旧条目；``snapshot`` 供 ``/dev/api/voice-log``。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

__all__ = ["VoiceLogEntry", "VoiceLog", "VOICE_LOG"]


@dataclass
class VoiceLogEntry:
    """单条语音交互事件（ts 为 wall clock 秒，页面本地化渲染）。"""

    ts: float
    kind: str  # state|wake|asr|playback|llm|llm_reply|llm_fail|speak|privacy
    text: str

    def to_json(self) -> dict[str, object]:
        return {"ts": self.ts, "kind": self.kind, "text": self.text}


class VoiceLog:
    """有界环形事件区（单事件循环线程内读写，无锁）。"""

    def __init__(self, capacity: int = 200) -> None:
        self._entries: deque[VoiceLogEntry] = deque(maxlen=capacity)

    def log(self, kind: str, text: str) -> None:
        self._entries.append(VoiceLogEntry(time.time(), kind, text))

    def snapshot(self) -> list[VoiceLogEntry]:
        """时间正序的全部条目（页面侧自行倒序渲染）。"""
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()


#: 进程级单例（模块 import 即用；测试可 clear 复位）
VOICE_LOG = VoiceLog()
