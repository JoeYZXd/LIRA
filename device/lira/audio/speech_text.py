"""Convert presentation markup to speech without changing words or numbers."""
import re

def spoken_text(text: str) -> str:
    text = re.sub(r'```[^\n]*\n?', '', text).replace('```', '')
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\\?\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+)', '', text)
    text = re.sub(r'(?m)^\s*\|?\s*:?-{3,}.*$', '', text)
    text = re.sub(r'(?m)^\s*(?:[-*_]\s*){3,}$', '', text)
    text = text.replace('\\*', '*').replace('**', '').replace('__', '').replace('`', '')
    text = re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)', r'\1', text)
    text = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'\1', text)
    text = text.replace('|', '，')
    return re.sub(r'\n{3,}', '\n\n', text).strip()
