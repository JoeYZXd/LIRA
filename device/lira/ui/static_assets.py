"""Content versions keep a new UI document from loading an old cached asset."""
from hashlib import sha256
from pathlib import Path


def asset_versions(*names: str) -> dict[str, str]:
    root = Path(__file__).with_name('static')
    return {name: sha256((root / name).read_bytes()).hexdigest()[:16] for name in names}
