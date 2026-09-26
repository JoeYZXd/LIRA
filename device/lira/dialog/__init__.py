"""LIRA 对话层（U3）：状态机 / 本地意图规则 / 三级路由 / 话术集中管理。

模块边界（计划 Output Structure）::

    dialog/state_machine.py  纯逻辑状态机（无 I/O，回调注入）
    dialog/intents.py        本地意图规则与词表（R20/R21/R23，Tier 1）
    dialog/router.py         三级路由：本地规则 → 本地 LLM → 远程 LLM（R11/R10）
    dialog/phrasebook.py     全部语音话术唯一出处（R24/R27/R28/R29，便于审校）
"""

from __future__ import annotations

from lira.dialog import phrasebook
from lira.dialog.intents import (
    CONFIRM_WORDS,
    CANCEL_WORDS,
    HELP_TRIGGER_WORDS,
    PLAYBACK_COMMANDS,
    PLAYBACK_WHITELIST_KEYWORDS_FILE,
    READ_TRIGGER_WORDS,
    REJECT_WORDS,
    Appliance,
    Intent,
    IntentKind,
    classify_confirmation,
    classify_appliance_miss,
    find_alias_hits,
    is_cancel,
    is_playback_command,
    match_local,
)
from lira.dialog.router import LlmHandler, RouteResult, Router, RouterTier
from lira.dialog.state_machine import AudioRoute, DialogCallbacks, DialogEngine, State

__all__ = [
    "phrasebook",
    # intents
    "CONFIRM_WORDS",
    "CANCEL_WORDS",
    "REJECT_WORDS",
    "PLAYBACK_COMMANDS",
    "READ_TRIGGER_WORDS",
    "HELP_TRIGGER_WORDS",
    "PLAYBACK_WHITELIST_KEYWORDS_FILE",
    "Appliance",
    "Intent",
    "IntentKind",
    "classify_confirmation",
    "is_cancel",
    "is_playback_command",
    "match_local",
    "classify_appliance_miss",
    "find_alias_hits",
    # router
    "LlmHandler",
    "RouteResult",
    "Router",
    "RouterTier",
    # state machine
    "AudioRoute",
    "DialogCallbacks",
    "DialogEngine",
    "State",
]
