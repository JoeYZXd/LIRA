#!/usr/bin/env python3
"""一次性下载 LIRA 推理模型到 `models/`（gitignore，不入库）。

下载内容（计划 U1 Approach / Key Decisions）：
  - sherpa-onnx KWS 唤醒词模型:   sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
  - sherpa-onnx 流式 ASR 模型:    sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20
  - sherpa-onnx TTS 模型:         matcha-icefall-zh-baker + vocos-22khz vocoder
  - PP-OCRv4 det/rec (onnx，x86 开发用；板上 .rknn 由 U10 经 rknn-toolkit2 转换)

用法::

    python3 models/download_models.py            # 全部下载
    python3 models/download_models.py --only kws tts
    python3 models/download_models.py --dest /path/to/models

URL 集中在本文件 `MODELS` 表中维护（版本锁定随 U10 板上核对更新）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = REPO_ROOT / "models"

SHERPA_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

MODELS: dict[str, dict[str, str]] = {
    "kws": {
        # 计划中的 zh-en-3M-2024-01-01 在 releases 不存在（404），
        # 换用当前唯一 zh-en KWS 资产（2025-12-20 版，同为 zipformer zh-en 3M）
        "url": f"{SHERPA_RELEASES}/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2",
        "desc": "sherpa-onnx KWS 唤醒词模型 (zipformer zh-en 3M)",
    },
    "asr": {
        "url": f"{SHERPA_RELEASES}/asr-models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20.tar.bz2",
        "desc": "sherpa-onnx 流式 ASR 模型 (zipformer bilingual zh-en)",
    },
    "tts": {
        "url": f"{SHERPA_RELEASES}/tts-models/matcha-icefall-zh-baker.tar.bz2",
        "desc": "sherpa-onnx TTS 模型 (matcha zh-baker)",
    },
    "vocoder": {
        # vocos 不在 tts-models（404），vocoder 独立发布于 vocoder-models（裸 onnx）
        "url": f"{SHERPA_RELEASES}/vocoder-models/vocos-22khz-univ.onnx",
        "desc": "matcha TTS 的 vocos 22kHz vocoder (univ)",
    },
    "ocr-det": {
        # rknn_model_zoo 已改为网盘分发（GitHub raw 路径 404），URL 取自其
        # examples/PPOCR/PPOCR-Det/model/download_model.sh
        "url": "https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/PPOCR/ppocrv4_det.onnx",
        "desc": "PP-OCRv4 检测模型 (onnx, x86 开发后端)",
    },
    "ocr-rec": {
        "url": "https://ftrg.zbox.filez.com/v2/delivery/data/95f00b0fc900458ba134f8b180b3f7a1/examples/PPOCR/ppocrv4_rec.onnx",
        "desc": "PP-OCRv4 识别模型 (onnx, x86 开发后端)",
    },
    "ocr-dict": {
        # CTC 字典，镜像 PaddlePaddle/PaddleOCR main 的 ppocr/utils/ppocr_keys_v1.txt
        "url": "https://cdn.jsdelivr.net/gh/PaddlePaddle/PaddleOCR@main/ppocr/utils/ppocr_keys_v1.txt",
        "desc": "PP-OCRv4 识别 CTC 字典 (6623 行)",
    },
}


def download(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / Path(url).name
    if target.exists():
        print(f"  已存在，跳过: {target.name}")
        return target
    print(f"  下载: {url}")

    def _report(blocks: int, block_size: int, total: int) -> None:
        if total > 0:
            percent = min(100, blocks * block_size * 100 // total)
            print(f"\r  进度: {percent}%", end="", flush=True)

    with urllib.request.urlopen(url) as resp, tempfile.NamedTemporaryFile(  # noqa: S310
        dir=dest_dir, delete=False
    ) as tmp:
        shutil.copyfileobj(resp, tmp, length=1024 * 1024)
        tmp_path = Path(tmp.name)
    print()
    tmp_path.replace(target)
    return target


def extract_tar_bz2(archive: Path, dest_dir: Path) -> None:
    if not archive.name.endswith((".tar.bz2", ".tbz2")):
        return
    print(f"  解压: {archive.name}")
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(dest_dir)  # noqa: S202 - 本地受控下载源
    archive.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        nargs="+",
        choices=sorted(MODELS),
        default=None,
        help="只下载指定模型组（默认全部）",
    )
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="下载目录")
    args = parser.parse_args(argv)

    selected = args.only if args.only else sorted(MODELS)
    print(f"模型目录: {args.dest}")
    failed: list[str] = []
    for key in selected:
        spec = MODELS[key]
        print(f"[{key}] {spec['desc']}")
        try:
            archive = download(spec["url"], args.dest)
            extract_tar_bz2(archive, args.dest)
        except Exception as exc:  # noqa: BLE001 - 下载脚本需对单项失败容错继续
            print(f"  失败: {exc}", file=sys.stderr)
            failed.append(key)
    if failed:
        print(f"\n以下模型下载失败: {', '.join(failed)}（检查网络/URL 表后重跑）", file=sys.stderr)
        return 1
    print("\n全部模型就绪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
