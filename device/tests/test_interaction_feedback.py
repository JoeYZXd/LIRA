from lira.dialog.interaction_feedback import waiting_cue


def test_local_cues_do_not_claim_search_or_completion():
    assert waiting_cue("上海明天天气") == "嗯。"
    assert waiting_cue("为什么天空是蓝色") == "嗯，我想想。"
    assert waiting_cue("你好") == "嗯。"


def test_cue_classification_has_a_bounded_input_window():
    assert waiting_cue("天" * 256 + "为什么") == "嗯。"
