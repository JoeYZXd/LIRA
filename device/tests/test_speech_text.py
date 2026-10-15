from lira.audio.speech_text import spoken_text

def test_markdown_symbols_are_not_spoken():
    assert spoken_text('## **ANTHROPIC**\n- `Claude`，气温 -2.5℃。') == 'ANTHROPIC\nClaude，气温 -2.5℃。'
    assert spoken_text(r'\*\*ANTHROPIC\*\*') == 'ANTHROPIC'

def test_links_keep_labels_without_reading_url():
    assert spoken_text('[气象局](https://example.com/weather)说有雨。') == '气象局说有雨。'

def test_plain_text_numbers_and_words_are_preserved():
    text='Hello, ANTHROPIC。药品每次 2.5 毫克，每天 3 次。'
    assert spoken_text(text) == text
