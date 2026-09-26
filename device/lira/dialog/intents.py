"""本地意图规则（U3）：词表 + 纯规则匹配，无 I/O、无模型依赖。

层级定位（计划 Key Technical Decisions「意图路由三级」）：
  - 本地规则层（本模块）：家电/阅读/帮助/取消/播放白名单，永远最先且必经——
    高危确认词表（R23）属于本层，**永不经 LLM**；
  - 本地 LLM / 远程 LLM：见 `router.py`（Phase 1 为注入接口，可配置降级）。

匹配纪律：
  - 确认/拒绝/取消/播放白名单一律**精确匹配**（fail-closed，模糊变体不生效）；
  - 家电别名命中多个设备时不猜（G12），返回 None（澄清话术由 U6 补）；
  - 播放白名单命令不进会话意图通道（R21/R11 结构性隔离的第一半）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "CONFIRM_WORDS",
    "REJECT_WORDS",
    "CANCEL_WORDS",
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
    "find_alias_hits",
    "classify_appliance_miss",
]

# ---------- 词表（R20/R23/R21，代码常量，本地安全底线的一部分） ----------

#: R20：任意态取消会话
CANCEL_WORDS = ("取消", "算了")

#: R23：高危确认集（fail-closed：精确匹配才生效）
CONFIRM_WORDS = ("确认", "是的", "好", "对", "开吧")

#: R23：高危拒绝集
REJECT_WORDS = ("取消", "不要", "不对")

#: R21/R22：朗读期间白名单命令（白名单 KWS 唯一输出的词集）
PLAYBACK_COMMANDS = ("暂停", "继续", "停止", "再读一遍", "大声点", "小声点")

#: 阅读意图触发词（子串匹配）
READ_TRIGGER_WORDS = ("读", "念")

#: 帮助指令触发词（子串匹配，R31）
HELP_TRIGGER_WORDS = ("你能做什么", "你能干啥", "你会做什么", "你会干啥", "帮助")

REPO_ROOT = Path(__file__).resolve().parents[3]
#: R21 白名单 KWS 关键词文件（sherpa KWS 拼音约定，见 assets/keywords/）
PLAYBACK_WHITELIST_KEYWORDS_FILE = (
    REPO_ROOT / "assets" / "keywords" / "playback_whitelist_raw.txt"
)


# ---------- 数据模型 ----------


class IntentKind(Enum):
    APPLIANCE = "appliance"
    READ = "read"
    HELP = "help"
    CANCEL = "cancel"
    PLAYBACK = "playback"


@dataclass
class Appliance:
    """家电条目（U6 会替换为持久化模型，字段名保持对齐）。"""

    name: str
    aliases: tuple[str, ...]
    actions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    is_high_risk: bool = False
    enabled: bool = True


@dataclass
class Intent:
    kind: IntentKind
    text: str = ""
    appliance: Appliance | None = None
    action: str | None = None


# ---------- 词表判定（精确匹配） ----------


def is_cancel(text: str) -> bool:
    return text.strip() in CANCEL_WORDS


def is_playback_command(text: str) -> bool:
    return text.strip() in PLAYBACK_COMMANDS


def classify_confirmation(text: str) -> str | None:
    """R23：返回 "confirm" / "reject" / None（None = 语义不明，fail-closed）。"""
    t = text.strip()
    if t in CONFIRM_WORDS:
        return "confirm"
    if t in REJECT_WORDS:
        return "reject"
    return None


# ---------- 本地规则匹配（Tier 1） ----------


def match_local(text: str, appliances: tuple[Appliance, ...]) -> Intent | None:
    """本地规则匹配；无命中返回 None（交给上层路由 Tier 2/3 或 R24 引导）。"""
    t = text.strip()
    if not t:
        return None
    if is_cancel(t):
        return Intent(IntentKind.CANCEL, text=t)
    if any(w in t for w in HELP_TRIGGER_WORDS):
        return Intent(IntentKind.HELP, text=t)
    if any(w in t for w in READ_TRIGGER_WORDS):
        return Intent(IntentKind.READ, text=t)
    appliance, action = _match_appliance(t, appliances)
    if appliance is not None and action is not None:
        return Intent(IntentKind.APPLIANCE, text=t, appliance=appliance, action=action)
    return None


def _match_appliance(
    t: str, appliances: tuple[Appliance, ...]
) -> tuple[Appliance | None, str | None]:
    """别名子串命中；命中多个设备不猜（G12）；动作取最长触发词。"""
    hits = find_alias_hits(t, appliances)
    if len(hits) != 1:
        return None, None
    appliance = hits[0]
    best_action: str | None = None
    best_trigger = ""
    for action, triggers in appliance.actions.items():
        for trig in triggers:
            if trig in t and len(trig) > len(best_trigger):
                best_action, best_trigger = action, trig
    if best_action is None:
        return None, None
    return appliance, best_action


def find_alias_hits(text: str, appliances: tuple[Appliance, ...]) -> list[Appliance]:
    """别名子串命中的全部设备（G12：多命中不猜，由状态机请求澄清）。"""
    t = text.strip()
    return [a for a in appliances if any(alias and alias in t for alias in a.aliases)]


def classify_appliance_miss(text: str, appliances: tuple[Appliance, ...]) -> str | None:
    """零别名命中时的细分（U6）：文本含任意已配置动作触发词（如"打开/关闭"）
    但设备别名零命中 → 指向了未配置设备，返回 "unconfigured"；否则 None
    （交给上层 R24 识别失败引导 / LLM 兜底）。
    """
    t = text.strip()
    if not t or find_alias_hits(t, appliances):
        return None
    for appliance in appliances:
        for triggers in appliance.actions.values():
            if any(trig and trig in t for trig in triggers):
                return "unconfigured"
    return None
