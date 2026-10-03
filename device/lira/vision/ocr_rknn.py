"""PP-OCRv4 mobile 板上后端（U10）：RKNNLite 直跑 det/rec `.rknn`。

与 `ocr_x86.OnnxOcrEngine` 同一 `OcrEngine` 接口，预处理/后处理逐环节对齐
rknn_model_zoo 板上路径：
  - det：输入为**原始 0-255**（ImageNet mean/std 已在转换时烘入模型），固定
    480x480 拉伸，NHWC；输出 (1,480,480,1) 概率图 → DB 最小后处理（与 x86 同参）；
  - rec：转换未烘 mean/std（零均值单位方差），输入归一化与 U4 onnx 路径一致：
    (x/255 - 0.5)/0.5、右侧补 -1 至 320px，NHWC；输出 (1,T,C) → CTC 贪心解码；
  - NPU 核绑定：det→core0、rec→core1（计划 U10 Approach）；绑定失败自动降级为
    默认调度。

与本文件刻意**互不依赖**：为保持 x86（onnxruntime）与板上（RKNNLite）两个后端
各自可独立部署，DB 后处理与 CTC 解码在两侧各自内置——均为 PaddleOCR 官方参考
实现，参数与 `ocr_x86.py` 逐项相同，改动需两侧同步。

隐私纪律：识别文本不落日志（引擎层不打印文本，冒烟脚本除外）。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pyclipper
from rknnlite.api import RKNNLite
from shapely.geometry import Polygon

from lira.vision.engine import OcrEngine

__all__ = ["RknnOcrEngine", "DET_RKNN", "REC_RKNN", "REC_DICT", "OCR_MODEL_HINT"]

REPO_ROOT = Path(__file__).resolve().parents[3]
MODELS_DIR = REPO_ROOT / "models"

DET_RKNN = MODELS_DIR / "ppocrv4_det.rknn"
REC_RKNN = MODELS_DIR / "ppocrv4_rec.rknn"
REC_DICT = MODELS_DIR / "ppocr_keys_v1.txt"
OCR_MODEL_HINT = (
    "OCR .rknn 模型缺失，应有 models/ppocrv4_det.rknn 与 models/ppocrv4_rec.rknn"
    "（x86 侧经 rknn-toolkit2 转换，见 deploy/SETUP.md 2.2）。"
)


class RknnOcrEngine(OcrEngine):
    """OcrEngine 的板上 RKNNLite 实现（det i8@core0 + rec fp16@core1）。"""

    #: det 静态输入（与 .rknn 转换约定一致：整图拉伸到固定尺寸）
    DET_SIZE = (480, 480)  # (h, w)
    #: 与 x86 后端同参（U4 定值，两侧同步维护）
    DET_THRESH = 0.3
    DET_BOX_THRESH = 0.6
    DET_UNCLIP_RATIO = 1.5
    REC_H = 48
    REC_W = 320

    def __init__(
        self,
        det_rknn: Path = DET_RKNN,
        rec_rknn: Path = REC_RKNN,
        rec_dict: Path = REC_DICT,
        core_det: int = 0,
        core_rec: int = 1,
    ) -> None:
        for path in (det_rknn, rec_rknn):
            if not path.is_file():
                raise FileNotFoundError(f"OCR .rknn 不存在: {path}。{OCR_MODEL_HINT}")
        if not rec_dict.is_file():
            raise FileNotFoundError(f"rec 字典不存在: {rec_dict}。{OCR_MODEL_HINT}")

        self._characters = _load_characters(rec_dict)
        self._det = self._load(det_rknn, core_det)
        self._rec = self._load(rec_rknn, core_rec)

    # ---------- OcrEngine 接口 ----------

    def detect(self, image: np.ndarray) -> list[np.ndarray]:
        """det：文本行四点多边形（原图坐标 float32 (4,2)，tl→tr→br→bl）。"""
        src_h, src_w = image.shape[:2]
        rh, rw = self.DET_SIZE
        # 原始 0-255 float32 NHWC——ImageNet mean/std 已烘入 .rknn（转换 config）
        img = cv2.resize(image, (rw, rh)).astype(np.float32)
        inp = np.ascontiguousarray(img[np.newaxis])
        out = self._det.inference(inputs=[inp])[0]
        prob = np.asarray(out).squeeze()  # (1,480,480,1) → (480,480)
        if prob.ndim != 2:
            raise RuntimeError(f"det 输出形状异常: {np.asarray(out).shape}")

        bitmap = (prob > self.DET_THRESH).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        ratio_h, ratio_w = rh / src_h, rw / src_w
        result: list[np.ndarray] = []
        for contour in contours[:1000]:
            points, sside = _mini_boxes(contour)
            if sside < 3:
                continue
            score = _box_score_fast(prob, points.reshape(-1, 2))
            if score < self.DET_BOX_THRESH:
                continue
            unclipped = _unclip(points, self.DET_UNCLIP_RATIO)
            if not len(unclipped):
                continue
            box, sside = _mini_boxes(unclipped)
            if sside < 5:
                continue
            box = _order_points_clockwise(box)
            box[:, 0] = np.clip(box[:, 0] / ratio_w, 0, src_w - 1)
            box[:, 1] = np.clip(box[:, 1] / ratio_h, 0, src_h - 1)
            if min(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[0] - box[3])) <= 3:
                continue
            result.append(box.astype(np.float32))
        return result

    def recognize(self, crop: np.ndarray) -> str:
        """rec：单行文本小图 → 文本（CTC 贪心解码）。"""
        if crop.size == 0:
            return ""
        h, w = crop.shape[:2]
        ratio = self.REC_H / h
        rw = min(int(np.ceil(w * ratio)), self.REC_W)
        resized = cv2.resize(crop, (rw, self.REC_H))
        x = (resized.astype(np.float32) / 255.0 - 0.5) / 0.5
        padded = np.full((self.REC_H, self.REC_W, 3), -1.0, dtype=np.float32)
        padded[:, :rw] = x
        inp = np.ascontiguousarray(padded[np.newaxis])  # (1,48,320,3) NHWC

        out = self._rec.inference(inputs=[inp])[0]
        probs = np.asarray(out).astype(np.float32)
        if probs.ndim == 3:  # (1, T, C)
            probs = probs[0]
        chars: list[str] = []
        prev = -1
        for idx in probs.argmax(axis=-1):
            if idx != prev and idx != 0:
                chars.append(self._characters[idx])
            prev = idx
        return "".join(chars)

    # ---------- 内部 ----------

    @staticmethod
    def _load(model_path: Path, core: int) -> RKNNLite:
        """加载 .rknn 并绑定 NPU 核；绑定失败降级为默认调度。"""
        lite = RKNNLite()
        if lite.load_rknn(str(model_path)) != 0:
            raise RuntimeError(f"load_rknn 失败: {model_path}")
        try:
            core_mask = getattr(RKNNLite, f"NPU_CORE_{core}")
            ret = lite.init_runtime(core_mask=core_mask)
        except (AttributeError, TypeError, ValueError):
            ret = lite.init_runtime()
        if ret != 0:
            raise RuntimeError(f"init_runtime 失败: {model_path}")
        return lite


# ---------- DB 后处理 / CTC（PaddleOCR 官方参考实现，与 ocr_x86.py 同参） ----------


def _load_characters(dict_path: Path) -> list[str]:
    """ppocr_keys_v1 语义：['blank'] + 字典行（首行可为空行）+ [' ']，与官方 CTCLabelDecode 一致。"""
    chars = []
    with dict_path.open("rb") as fh:
        for raw in fh.readlines():
            chars.append(raw.decode("utf-8").strip("\n").strip("\r\n"))
    chars.append(" ")
    return ["blank"] + chars


def _mini_boxes(contour: np.ndarray) -> tuple[np.ndarray, float]:
    rect = cv2.minAreaRect(contour)
    points = sorted(list(cv2.boxPoints(rect)), key=lambda x: x[0])
    i1, i4 = (0, 1) if points[1][1] > points[0][1] else (1, 0)
    i2, i3 = (2, 3) if points[3][1] > points[2][1] else (3, 2)
    return np.array([points[i1], points[i2], points[i3], points[i4]]), float(min(rect[1]))


def _unclip(box: np.ndarray, unclip_ratio: float) -> np.ndarray:
    poly = Polygon(box)
    if poly.area <= 0 or poly.length <= 0:
        return np.empty((0, 2))
    distance = poly.area * unclip_ratio / poly.length
    offset = pyclipper.PyclipperOffset()
    offset.AddPath(box.tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    return np.array(offset.Execute(distance))


def _box_score_fast(bitmap: np.ndarray, box: np.ndarray) -> float:
    """框内 prob 均值（官方 box_score_fast）。"""
    h, w = bitmap.shape[:2]
    box = box.copy()
    x_min = int(np.clip(np.floor(box[:, 0].min()), 0, w - 1))
    x_max = int(np.clip(np.ceil(box[:, 0].max()), 0, w - 1))
    y_min = int(np.clip(np.floor(box[:, 1].min()), 0, h - 1))
    y_max = int(np.clip(np.ceil(box[:, 1].max()), 0, h - 1))
    mask = np.zeros((y_max - y_min + 1, x_max - x_min + 1), dtype=np.uint8)
    box[:, 0] -= x_min
    box[:, 1] -= y_min
    cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
    return float(cv2.mean(bitmap[y_min:y_max + 1, x_min:x_max + 1], mask)[0])


def _order_points_clockwise(pts: np.ndarray) -> np.ndarray:
    """四点排序为 tl→tr→br→bl（官方 DetPostProcess.order_points_clockwise）。"""
    x_sorted = pts[np.argsort(pts[:, 0]), :]
    left = x_sorted[:2][np.argsort(x_sorted[:2, 1])]
    right = x_sorted[2:][np.argsort(x_sorted[2:, 1])]
    return np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)
