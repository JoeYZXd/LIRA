"""U3 本地意图规则测试（词表 + 本地规则匹配）。

覆盖：R23 确认/拒绝词表（fail-closed：仅精确匹配）、R20 取消词表、
R21/R22 朗读白名单命令集、本地规则（家电/阅读/帮助）、多别名歧义不猜（G12）、
assets/keywords/playback_whitelist_raw.txt 文件格式（sherpa KWS 约定）。
"""

from __future__ import annotations

from pathlib import Path

from lira.dialog.intents import (
    CONFIRM_WORDS,
    CANCEL_WORDS,
    PLAYBACK_COMMANDS,
    PLAYBACK_WHITELIST_KEYWORDS_FILE,
    REJECT_WORDS,
    READ_TRIGGER_WORDS,
    Appliance,
    IntentKind,
    classify_confirmation,
    is_cancel,
    is_playback_command,
    match_local,
)


def make_appliances() -> tuple[Appliance, ...]:
    return (
        Appliance(
            name="取暖器",
            aliases=("取暖器",),
            actions={"打开": ("打开", "开一下"), "关闭": ("关闭", "关掉")},
            is_high_risk=True,
        ),
        Appliance(
            name="台灯",
            aliases=("台灯",),
            actions={"打开": ("打开",), "关闭": ("关闭",)},
            is_high_risk=False,
        ),
        Appliance(
            name="加湿器",
            aliases=("加湿器",),
            actions={"打开": ("打开",)},
            is_high_risk=False,
            enabled=False,
        ),
    )


# ---------- R23 高危确认词表 ----------


class TestConfirmationWordLists:
    def test_confirm_set_exact_contents(self):
        assert set(CONFIRM_WORDS) == {"确认", "是的", "好", "对", "开吧"}

    def test_reject_set_exact_contents(self):
        assert set(REJECT_WORDS) == {"取消", "不要", "不对"}

    def test_classify_confirm(self):
        for w in ("确认", "是的", "好", "对", "开吧"):
            assert classify_confirmation(w) == "confirm", w

    def test_classify_reject(self):
        for w in ("取消", "不要", "不对"):
            assert classify_confirmation(w) == "reject", w

    def test_unrelated_answer_is_none(self):
        """AE3 error path：无关应答既不是确认也不是拒绝。"""
        assert classify_confirmation("今天天气怎么样") is None

    def test_fail_closed_on_near_miss(self):
        """fail-closed：仅精确匹配，模糊变体不算确认。"""
        assert classify_confirmation("好的") is None
        assert classify_confirmation("对吧") is None
        assert classify_confirmation("确认一下") is None


# ---------- R20 取消词表 / R21 播放白名单 ----------


class TestCancelAndPlaybackWords:
    def test_cancel_words(self):
        assert set(CANCEL_WORDS) == {"取消", "算了"}
        assert is_cancel("取消")
        assert is_cancel("算了")
        assert not is_cancel("取消一下")  # 精确匹配

    def test_playback_whitelist_exact_set(self):
        assert set(PLAYBACK_COMMANDS) == {"暂停", "继续", "停止", "再读一遍", "大声点", "小声点"}
        for w in PLAYBACK_COMMANDS:
            assert is_playback_command(w)
        # AE4：白名单外文本一律不是播放命令
        assert not is_playback_command("打开电视")
        assert not is_playback_command("暂停一下")

    def test_read_trigger_words(self):
        assert any(w in "帮我读一下这个" for w in READ_TRIGGER_WORDS)


# ---------- 本地规则匹配 ----------


class TestLocalRules:
    def test_appliance_high_risk_match(self):
        intent = match_local("打开取暖器", make_appliances())
        assert intent is not None
        assert intent.kind is IntentKind.APPLIANCE
        assert intent.appliance is not None
        assert intent.appliance.name == "取暖器"
        assert intent.appliance.is_high_risk is True
        assert intent.action == "打开"

    def test_appliance_low_risk_match(self):
        intent = match_local("打开台灯", make_appliances())
        assert intent is not None and intent.kind is IntentKind.APPLIANCE
        assert intent.appliance is not None and intent.appliance.name == "台灯"
        assert intent.action == "打开"

    def test_disabled_appliance_still_matches(self):
        """禁用状态的匹配由状态机拒绝并播报（AE3），规则层照常匹配。"""
        intent = match_local("打开加湿器", make_appliances())
        assert intent is not None
        assert intent.appliance is not None
        assert intent.appliance.enabled is False

    def test_ambiguous_alias_does_not_guess(self):
        """G12：两个设备别名同时命中 → 不猜，返回 None（U6 再补澄清话术）。"""
        ambiguous = (
            Appliance(name="台灯", aliases=("灯",), actions={"打开": ("打开",)}),
            Appliance(name="吊灯", aliases=("灯",), actions={"打开": ("打开",)}),
        )
        assert match_local("打开灯", ambiguous) is None

    def test_read_intent(self):
        intent = match_local("帮我读一下这个", make_appliances())
        assert intent is not None and intent.kind is IntentKind.READ

    def test_help_intent(self):
        intent = match_local("你能做什么", make_appliances())
        assert intent is not None and intent.kind is IntentKind.HELP

    def test_cancel_intent(self):
        intent = match_local("算了", make_appliances())
        assert intent is not None and intent.kind is IntentKind.CANCEL

    def test_no_match_returns_none(self):
        assert match_local("今天天气怎么样", make_appliances()) is None

    def test_playback_command_is_not_local_command(self):
        """白名单命令不进会话意图通道（只在 READING 态由白名单 KWS 处理）。"""
        assert match_local("暂停", make_appliances()) is None


# ---------- 白名单 KWS 关键词文件（sherpa KWS 约定） ----------


class TestPlaybackWhitelistKeywordsFile:
    def test_file_exists_and_format(self):
        path: Path = PLAYBACK_WHITELIST_KEYWORDS_FILE
        assert path.is_file(), f"白名单关键词文件缺失: {path}"
        lines = [ln.strip() for ln in path.read_text("utf-8").splitlines() if ln.strip()]
        words = set()
        for ln in lines:
            # 约定：声母韵母拆分 + @词，无 # 注释行（与 wakeword_raw.txt 同格式）
            assert "#" not in ln
            assert "@" in ln
            pinyin, _, word = ln.partition("@")
            assert pinyin.split(), f"缺拼音列: {ln}"
            words.add(word)
        assert words == set(PLAYBACK_COMMANDS)
