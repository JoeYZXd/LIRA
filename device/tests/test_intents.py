"""U3 本地意图规则测试（词表 + 本地规则匹配）。

覆盖：R23 确认/拒绝词表（fail-closed：仅精确匹配）、R20 取消词表、
R21/R22 朗读白名单命令集、本地规则（家电/阅读/帮助）、多别名歧义不猜（G12）、
assets/keywords/playback_whitelist_raw.txt 文件格式（sherpa KWS 约定）。
"""

from __future__ import annotations

from pathlib import Path

from lira.dialog.intents import (
    COMMAND_KWS_WORDS,
    COMMANDS_KEYWORDS_FILE,
    COMMANDS_RAW_FILE,
    CONFIRM_WORDS,
    CANCEL_WORDS,
    EXIT_STOP_WORDS,
    PLAYBACK_COMMANDS,
    PLAYBACK_WHITELIST_KEYWORDS_FILE,
    REJECT_WORDS,
    READ_TRIGGER_WORDS,
    VOLUME_DOWN_WORDS,
    VOLUME_UP_WORDS,
    Appliance,
    IntentKind,
    classify_confirmation,
    is_cancel,
    is_exit_or_stop,
    is_playback_command,
    is_repeat_word,
    is_shutdown_phrase,
    is_status_query,
    match_dnd_phrase,
    match_local,
    match_volume_word,
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

    def test_playback_command_precedes_read_trigger(self):
        """恢复阅读命令在新聆听窗口仍属播放控制，不触发新的拍摄。"""
        for word in PLAYBACK_COMMANDS:
            intent = match_local(word, make_appliances())
            assert intent is not None and intent.kind is IntentKind.PLAYBACK

    def test_bare_book_fragment_requires_local_clarification(self):
        from lira.dialog.intents import is_book_fragment

        for text in ("这本书", "这本书。", "这一页", "这页文字"):
            assert is_book_fragment(text)
        for text in ("帮我读下这本书", "这本书讲什么", "打开台灯", "再读一遍"):
            assert not is_book_fragment(text)


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


# ---------- 退出/勿扰/控制词表（2026-10-06 计划 U1） ----------


class TestExitStopWords:
    def test_full_set_hits(self):
        for w in EXIT_STOP_WORDS:
            assert is_exit_or_stop(w), w

    def test_punctuation_stripped(self):
        assert is_exit_or_stop("行了。")
        assert is_exit_or_stop("别念了，")

    def test_homophone_misrecognition_hits(self):
        """拼音归一：同音误转写与正字全等命中。"""
        assert is_exit_or_stop("别年了")  # 别念了
        assert is_exit_or_stop("闭最")  # 闭嘴（bi zui）

    def test_fail_closed(self):
        """候选集外一律不命中（无编辑距离放宽）。"""
        assert not is_exit_or_stop("别吵")
        assert not is_exit_or_stop("吵架")
        assert not is_exit_or_stop("退出吧")
        assert not is_exit_or_stop("今天天气怎么样")

    def test_guanji_is_exit_not_shutdown(self):
        """R5：「关机」只作结束会话，永不构成真关机。"""
        assert is_exit_or_stop("关机")
        assert not is_shutdown_phrase("关机")


class TestShutdownPhrase:
    def test_hit(self):
        assert is_shutdown_phrase("立即去休息")
        assert is_shutdown_phrase("立即去休息。")

    def test_fail_closed(self):
        for t in ("休息一下", "休息休息", "该休息了", "不想休息"):
            assert not is_shutdown_phrase(t), t

    def test_asr_variant_candidates(self):
        """e2e 实测变体入候选集（真实 ASR 转写「立即去休息」→「一起去休息」）。"""
        assert is_shutdown_phrase("一起去休息")
        assert is_shutdown_phrase("立即休息")


class TestDndPhrases:
    def test_enter(self):
        for t in ("进入勿扰模式", "进入勿扰", "开启勿扰模式"):
            assert match_dnd_phrase(t) == "enter", t

    def test_exit(self):
        for t in ("退出勿扰模式", "退出勿扰", "关闭勿扰模式"):
            assert match_dnd_phrase(t) == "exit", t

    def test_query(self):
        for t in ("你现在是在勿扰模式吗", "在睡觉吗", "你是不是睡着了"):
            assert match_dnd_phrase(t) == "query", t

    def test_fail_closed(self):
        assert match_dnd_phrase("勿扰模式") is None
        assert match_dnd_phrase("我不困") is None


class TestVolumeRepeatStatus:
    def test_volume(self):
        assert match_volume_word("声音大点") == "up"
        assert match_volume_word("声音小一点") == "down"
        assert match_volume_word("大声点") is None  # 白名单词，刻意不在此表

    def test_repeat(self):
        for t in ("再说一遍", "再说一次", "没听清", "没听清楚"):
            assert is_repeat_word(t), t
        assert not is_repeat_word("再说一遍吧")  # fail-closed

    def test_status_query(self):
        for t in ("你连上网了吗", "网络怎么样", "你的云端能连上吗", "连wifi了吗"):
            assert is_status_query(t), t
        for t in ("今天天气怎么样", "打开电视"):
            assert not is_status_query(t), t


class TestWordSetDisjointness:
    def test_no_overlap_with_legacy_sets(self):
        assert not set(EXIT_STOP_WORDS) & set(CANCEL_WORDS)
        assert not set(EXIT_STOP_WORDS) & set(PLAYBACK_COMMANDS)
        volume = set(VOLUME_UP_WORDS) | set(VOLUME_DOWN_WORDS)
        assert not volume & set(PLAYBACK_COMMANDS)

    def test_command_kws_subset_of_exit_stop(self):
        """声学子集是文本层全集的子集（词表统一裁定的声学收窄）。"""
        assert set(COMMAND_KWS_WORDS) <= set(EXIT_STOP_WORDS)


class TestCommandsKeywordsFile:
    def test_file_exists_and_format(self):
        """commands.txt 独立断言分支：允许行内 :boost #threshold 参数
        （区别于白名单文件的「无 #」约定），# 后必须跟数字。"""
        import re

        path = COMMANDS_KEYWORDS_FILE
        assert path.is_file(), f"命令关键词文件缺失: {path}"
        lines = [ln.strip() for ln in path.read_text("utf-8").splitlines() if ln.strip()]
        words = set()
        for ln in lines:
            assert "@" in ln, ln
            for m in re.finditer(r"#(.)", ln):
                assert m.group(1).replace(".", "").isdigit(), f"# 必须是阈值参数: {ln}"
            pinyin, _, word = ln.rpartition("@")
            assert pinyin.split(), f"缺拼音列: {ln}"
            words.add(word.strip())
        assert words == set(COMMAND_KWS_WORDS)

    def test_raw_source_in_sync(self):
        raw_lines = [
            ln.strip()
            for ln in COMMANDS_RAW_FILE.read_text("utf-8").splitlines()
            if ln.strip()
        ]
        raw_words = {ln.rpartition("@")[2].strip() for ln in raw_lines}
        assert raw_words == set(COMMAND_KWS_WORDS)
