"""本地意图规则（U3）：词表 + 纯规则匹配，无 I/O、无模型依赖。

层级定位（计划 Key Technical Decisions「意图路由三级」）：
  - 本地规则层（本模块）：家电/阅读/帮助/取消/播放白名单，永远最先且必经——
    高危确认词表（R23）属于本层，**永不经 LLM**；
  - 本地 LLM / 远程 LLM：见 `router.py`（Phase 1 为注入接口，可配置降级）。

匹配纪律：
  - 确认/拒绝/取消/播放白名单一律**精确匹配**（fail-closed，模糊变体不生效）；
  - 家电别名命中多个设备时不猜（G12），返回 None（澄清话术由 U6 补）；
  - 播放白名单优先于阅读触发词；是否有可恢复任务由状态机判定。
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
    "EXIT_STOP_WORDS",
    "SHUTDOWN_PHRASES",
    "DND_ENTER_PHRASES",
    "DND_EXIT_PHRASES",
    "DND_QUERY_PHRASES",
    "VOLUME_UP_WORDS",
    "VOLUME_DOWN_WORDS",
    "REPEAT_WORDS",
    "STATUS_QUERY_TOPIC_WORDS",
    "COMMAND_KWS_WORDS",
    "COMMANDS_KEYWORDS_FILE",
    "COMMANDS_RAW_FILE",
    "Appliance",
    "Intent",
    "IntentKind",
    "classify_confirmation",
    "is_cancel",
    "is_playback_command",
    "is_book_fragment",
    "is_exit_or_stop",
    "is_shutdown_phrase",
    "match_dnd_phrase",
    "match_volume_word",
    "is_repeat_word",
    "is_status_query",
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

# ---------- 退出/勿扰/控制词表（2026-10-06 计划；封闭候选集 + 拼音归一匹配） ----------
#
# 匹配纪律（计划 Key Technical Decisions）：封闭候选集内拼音归一全等命中——
# 同音误转写（如「别年了」→「别念了」）归一后自然命中；候选集外一律不命中
# （fail-closed），不做编辑距离等放宽（留待按 ASR 实际误转写样本评估）。

#: R1/R6 统一退出∪喊停词集（ASR 文本层全集）：会话中任何非待机态命中即结束会话
#: 回待机。其中「关机」按 R5 只作结束会话——永不匹配 SHUTDOWN、不进家电分支。
#: 播报中喊停走命令 KWS 声学子集（COMMAND_KWS_WORDS，见下）：两字词（退出/关机）
#: 在朗读回声环境误触发面大，不进声学集——「词表统一」裁定在声学层的收窄见计划 U1。
EXIT_STOP_WORDS = (
    "行了", "行了吧", "别吵我", "别念了", "别说了", "行了行了", "够了", "闭嘴",
    "退出", "关机",
)

#: R4：真关机短语（仅唤醒前缀投递生效——前缀标记由 route_dispatch 随投递携带）
SHUTDOWN_PHRASES = ("立即去休息",)

#: R8：勿扰进入短语（候选集；会话中裸说亦生效，见 R13）
DND_ENTER_PHRASES = ("进入勿扰模式", "开启勿扰模式", "进入勿扰")

#: R11：勿扰退出短语（勿扰中裸说亦生效）
DND_EXIT_PHRASES = ("退出勿扰模式", "关闭勿扰模式", "退出勿扰")

#: R12：勿扰状态询问（同义问法候选集；勿扰内外皆答）
DND_QUERY_PHRASES = (
    "你现在是在勿扰模式吗", "你在勿扰模式吗", "在勿扰模式吗",
    "你是不是在勿扰", "是不是在勿扰模式", "你是不是在睡觉", "你是不是睡着了",
    "你在睡觉吗", "在睡觉吗", "你是不是在休息",
)

#: R16：全局音量语音词。与朗读白名单的「大声点/小声点」刻意不重叠——
#: 白名单词走 READING 播放通道（音量语义并轨见计划 U8），本表走 ASR 文本层。
VOLUME_UP_WORDS = ("声音大点", "声音大一点")
VOLUME_DOWN_WORDS = ("声音小点", "声音小一点")

#: R15：追问窗内重复上答
REPEAT_WORDS = ("再说一遍", "再说一次", "没听清", "没听清楚")

#: R17：状态自查话题词根（文本含任一词根即视为状态询问；封闭词根集，
#: 勿扰中豁免可答）。单字「网」过宽不入表。
STATUS_QUERY_TOPIC_WORDS = (
    "网络", "上网", "连网", "联网", "云端", "麦克风", "信号", "wifi",
)

#: 命令 KWS 声学子集（assets/keywords/commands.txt 的 @词全集，一致性由测试钉死）。
#: 全部多音节高区分度短语；阈值逐词 :boost #threshold 参数（U10 板上定稿）。
COMMAND_KWS_WORDS = ("别吵我", "别念了", "别说了", "行了", "行了行了", "够了", "闭嘴")

#: 命令 KWS 词文件（token 格式，生成源为 commands_raw.txt）
COMMANDS_KEYWORDS_FILE = REPO_ROOT / "assets" / "keywords" / "commands.txt"
#: 命令 KWS 词源文件（raw 格式：词条 + :boost #threshold @原词；
#: 重新生成：sherpa-onnx-cli text2token --tokens <model>/tokens.txt
#:   --tokens-type phone+ppinyin --lexicon <model>/en.phone
#:   commands_raw.txt commands.txt，生成后人工核对多音字拼音行）
COMMANDS_RAW_FILE = REPO_ROOT / "assets" / "keywords" / "commands_raw.txt"


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


def is_book_fragment(text: str) -> bool:
    """Object-only book/page fragments need a local question before any action."""
    return text.strip().rstrip("，,。.!！?？").strip() in {
        "这本书", "这本", "这页", "这一页", "这页书", "这页文字", "这页内容",
    }


def classify_confirmation(text: str) -> str | None:
    """R23：返回 "confirm" / "reject" / None（None = 语义不明，fail-closed）。"""
    t = text.strip()
    if t in CONFIRM_WORDS:
        return "confirm"
    if t in REJECT_WORDS:
        return "reject"
    return None


# ---------- 退出/勿扰/控制判定（拼音归一 + 封闭候选集） ----------

_SOFT_PUNCT = set("，,.。!！?？、;；:：'\"“”‘’()（）[]【】 \t\n\r")

_PINYIN_KEY_CACHE: dict[str, str] = {}


def _pinyin_key(text: str) -> str:
    """无声调全拼连写归一。同音误转写归一后与正字全等（「别年了」==「别念了」）；
    容错边界即候选集拼音表，不做编辑距离放宽（fail-closed）。"""
    cached = _PINYIN_KEY_CACHE.get(text)
    if cached is not None:
        return cached
    from pypinyin import lazy_pinyin  # 延迟导入：本模块是热路径，不背启动开销

    key = "".join(lazy_pinyin("".join(ch for ch in text if ch not in _SOFT_PUNCT)))
    _PINYIN_KEY_CACHE[text] = key
    return key


def _match_closed(text: str, candidates: tuple[str, ...]) -> str | None:
    """拼音归一全等命中候选集之一；候选集外一律 None。"""
    key = _pinyin_key(text)
    if not key:
        return None
    for cand in candidates:
        if key == _pinyin_key(cand):
            return cand
    return None


def is_exit_or_stop(text: str) -> bool:
    """R1/R6：统一退出∪喊停集命中（含「关机」——语义为结束会话，见词表注释）。"""
    return _match_closed(text, EXIT_STOP_WORDS) is not None


def is_shutdown_phrase(text: str) -> bool:
    """R4：真关机短语命中（调用方须另验唤醒前缀标记）。"""
    return _match_closed(text, SHUTDOWN_PHRASES) is not None


def match_dnd_phrase(text: str) -> str | None:
    """R8/R11/R12：返回 "enter" / "exit" / "query" / None（候选集间互斥，
    依次判定 enter → exit → query）。"""
    if _match_closed(text, DND_ENTER_PHRASES):
        return "enter"
    if _match_closed(text, DND_EXIT_PHRASES):
        return "exit"
    if _match_closed(text, DND_QUERY_PHRASES):
        return "query"
    return None


def match_volume_word(text: str) -> str | None:
    """R16：返回 "up" / "down" / None。"""
    if _match_closed(text, VOLUME_UP_WORDS):
        return "up"
    if _match_closed(text, VOLUME_DOWN_WORDS):
        return "down"
    return None


def is_repeat_word(text: str) -> bool:
    """R15：追问窗内重复上答命中。"""
    return _match_closed(text, REPEAT_WORDS) is not None


def is_status_query(text: str) -> bool:
    """R17：状态自查询问（封闭词根集子串命中）。"""
    t = text.strip().lower()
    if not t:
        return False
    return any(topic in t for topic in STATUS_QUERY_TOPIC_WORDS)


# ---------- 本地规则匹配（Tier 1） ----------


def match_local(text: str, appliances: tuple[Appliance, ...]) -> Intent | None:
    """本地规则匹配；无命中返回 None（交给上层路由 Tier 2/3 或 R24 引导）。"""
    t = text.strip()
    if not t:
        return None
    if is_cancel(t):
        return Intent(IntentKind.CANCEL, text=t)
    if is_playback_command(t):
        return Intent(IntentKind.PLAYBACK, text=t)
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
