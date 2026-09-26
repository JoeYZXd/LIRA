"""mock 摄像头：按顺序轮询读取 `assets/mock_images/` 中的图片文件。

"连拍两图返回不同文件内容"由轮询语义自然保证（U1 测试场景 3）。
"""

from __future__ import annotations

from pathlib import Path

from lira.hal.base import Camera, HalError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


class MockCamera(Camera):
    """从目录读取预置图片模拟拍摄。

    Args:
        images_dir: 图片目录（默认 assets/mock_images）。目录内按文件名排序轮询。
    """

    def __init__(self, images_dir: Path) -> None:
        self.images_dir = Path(images_dir)
        self._images: list[Path] = []
        self._index = 0
        self.capture_count = 0

    async def open(self) -> None:
        if not self.images_dir.is_dir():
            raise HalError(f"mock 摄像头图片目录不存在: {self.images_dir}")
        self._images = sorted(
            p for p in self.images_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES
        )
        if not self._images:
            raise HalError(
                f"mock 摄像头图片目录为空（需放入 jpg/png 样张）: {self.images_dir}"
            )
        self._index = 0

    async def close(self) -> None:
        self._images = []

    async def capture(self) -> bytes:
        if not self._images:
            raise HalError("mock 摄像头未打开（请用作 async 上下文管理器）。")
        path = self._images[self._index % len(self._images)]
        self._index += 1
        self.capture_count += 1
        return path.read_bytes()

    @property
    def image_names(self) -> list[str]:
        """当前轮询的图片文件名（装配图/dry-run 展示用）。"""
        return [p.name for p in self._images]
