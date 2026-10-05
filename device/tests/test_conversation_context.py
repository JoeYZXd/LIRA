"""Completed voice turns remain bounded, immutable and independent of book text."""
from dataclasses import FrozenInstanceError

import pytest

from lira.dialog.context import (
    ConversationHistory, ReadingTaskFacts, ReadingTaskStatus,
)


def test_history_evicts_whole_oldest_pairs():
    history = ConversationHistory(max_pairs=2, max_chars=20)
    history.record_completed("甲", "答甲")
    history.record_completed("乙", "答乙")
    history.record_completed("丙", "答丙")
    assert [(p.user, p.assistant) for p in history.snapshot().pairs] == [("乙", "答乙"), ("丙", "答丙")]


def test_character_budget_never_retains_partial_pair():
    history = ConversationHistory(max_pairs=6, max_chars=8)
    history.record_completed("123", "456")
    history.record_completed("ab", "cd")
    assert history.snapshot().messages() == [
        {"role": "user", "content": "ab"}, {"role": "assistant", "content": "cd"},
    ]
    history.record_completed("too-long-user", "answer")
    assert history.snapshot().pairs == ()


def test_default_budget_is_six_pairs_and_four_thousand_characters():
    history = ConversationHistory()
    for i in range(8):
        history.record_completed(str(i), "answer")
    assert [p.user for p in history.snapshot().pairs] == [str(i) for i in range(2, 8)]
    history.record_completed("a" * 2000, "b" * 2000)
    assert len(history.snapshot().pairs) == 1


def test_snapshot_is_immutable_and_clear_does_not_rewrite_inflight_call():
    history = ConversationHistory()
    history.record_completed("问题", "答案")
    facts = ReadingTaskFacts(status=ReadingTaskStatus.PAUSED, resumable=True)
    old = history.snapshot(task=facts)
    history.clear()
    assert history.snapshot().pairs == ()
    assert old.pairs[0].user == "问题" and old.task == facts
    with pytest.raises(FrozenInstanceError):
        old.pairs[0].assistant = "changed"


def test_empty_turn_is_not_recorded():
    history = ConversationHistory()
    history.record_completed("", "answer")
    history.record_completed("question", " ")
    assert history.snapshot().pairs == ()


def test_task_facts_have_no_ocr_or_scene_fields():
    facts = ReadingTaskFacts(status=ReadingTaskStatus.PAUSED, resumable=True)
    assert facts.kind == "reading"
    assert set(facts.as_dict()) == {"kind", "status", "resumable"}
    assert facts.as_dict()["status"] == "paused"


@pytest.mark.parametrize("max_pairs,max_chars", [(0, 1), (1, 0), (-1, 8)])
def test_invalid_bounds_are_rejected(max_pairs, max_chars):
    with pytest.raises(ValueError):
        ConversationHistory(max_pairs=max_pairs, max_chars=max_chars)
