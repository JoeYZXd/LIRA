"""PP-OCRv4 mobile x86 后端（U4）：onnxruntime 直跑 det/rec，最小依赖预处理/后处理。

与 airockchip/rknn_model_zoo `examples/PPOCR/` 的 onnx 路径逐环节对齐
（板上 .rknn 由同一 onnx 转换而来，U10 时仅换执行器）：
  - det 预处理：长边 480px（`limit_type=max`，尺寸对齐 32 倍数，Key Decisions），
    ImageNet mean/std 归一化；det 后处理：DB 阈值 0.3 / box 阈值 0.6 /
    unclip 1.5（pyclipper + shapely，与官方 DBPostProcess 一致的最小实现）；
  - rec 预处理：高 48px 等比缩放、右侧补 -1（归一化后补零）到 ≤320px；
    后处理：CTC 贪心解码（blank=0、重复折叠），字典 ppocr_keys_v1.txt + 空格。

不引入 paddle 生态（计划 U4 Approach）；隐私纪律：识别文本不落日志。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import pyclipper
from shapely.geometry import Polygon

from lira.vision.engine import OcrEngine

__all__ = [
    "OnnxOcrEngine",
    "DET_ONNX",
    "REC_ONNX",
    "REC_DICT",
    "OCR_MODEL_DOWNLOAD_HINT",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
MODELS_DIR = REPO_ROOT / "models"

DET_ONNX = MODELS_DIR / "ppocrv4_det.onnx"
REC_ONNX = MODELS_DIR / "ppocrv4_rec.onnx"
REC_DICT = MODELS_DIR / "ppocr_keys_v1.txt"

# download_models.py 中旧 URL 已 404（模型迁移到 rknn_model_zoo 网盘分发）；
# 当前有效地址（已报告，待 download_models.py 表更新）：
OCR_MODEL_DOWNLOAD_HINT = (
    "OCR 模型缺失。请在 models/ 目录执行："
    "curl -LO 'https://ftrg.zbox.filez.com/v2/delivery/data/"
    "95f00b0fc900458ba134f8b180b3f7a1/examples/PPOCR/ppocrv4_det.onnx'，"
    "同法下载 ppocrv4_rec.onnx；字典："
    "https://raw.githubusercontent.com/airockchip/rknn_model_zoo/main/"
    "examples/PPOCR/PPOCR-Rec/model/ppocr_keys_v1.txt"
)


class OnnxOcrEngine(OcrEngine):
    """OcrEngine 的 x86 onnxruntime 实现（det 480px 长边，与板上约定对齐）。"""

    #: Key Decisions：det 长边限制（调优空间预留 480→736，见计划 Risk 表）
    DET_LIMIT_SIDE_LEN = 480
    DET_THRESH = 0.3
    DET_BOX_THRESH = 0.6
    DET_UNCLIP_RATIO = 1.5
    REC_IMAGE_HEIGHT = 48
    REC_MAX_WIDTH = 320
    #: ImageNet 归一化（det，rknn_model_zoo onnx 路径约定）
    DET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    DET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def __init__(
        self,
        det_onnx: Path = DET_ONNX,
        rec_onnx: Path = REC_ONNX,
        rec_dict: Path = REC_DICT,
        num_threads: int = 2,
    ) -> None:
        for path in (det_onnx, rec_onnx):
            if not path.is_file():
                raise FileNotFoundError(f"OCR 模型不存在: {path}。{OCR_MODEL_DOWNLOAD_HINT}")
        if not rec_dict.is_file():
            raise FileNotFoundError(f"rec 字典不存在: {rec_dict}。{OCR_MODEL_DOWNLOAD_HINT}")

        options = ort.SessionOptions()
        options.intra_op_num_threads = num_threads
        self._det = ort.InferenceSession(str(det_onnx), options, providers=["CPUExecutionProvider"])
        self._rec = ort.InferenceSession(str(rec_onnx), options, providers=["CPUExecutionProvider"])
        self._det_input = self._det.get_inputs()[0].name
        self._rec_input = self._rec.get_inputs()[0].name
        det_shape = self._det.get_inputs()[0].shape  # [1,3,H,W]，H/W 可能为字符串（动态）
        self._det_size: tuple[int, int] | None = (
            (int(det_shape[2]), int(det_shape[3]))
            if isinstance(det_shape[2], int) and isinstance(det_shape[3], int)
            else None
        )
        self._characters = self._load_characters(rec_dict)
        rec_dim = self._rec.get_outputs()[0].shape[-1]
        if len(self._characters) != rec_dim:
            raise ValueError(
                f"rec 字典长度 {len(self._characters)} 与模型输出类别数 {rec_dim} 不一致，"
                f"请核对 {rec_dict} 是否为 ppocr_keys_v1.txt。"
            )

    # ---------- OcrEngine 接口 ----------

    def detect(self, image: np.ndarray) -> list[np.ndarray]:
        """det：文本行四点多边形（原图坐标 float32 (4,2)，tl→tr→br→bl）。"""
        src_h, src_w = image.shape[:2]
        img, ratio_h, ratio_w = self._det_preprocess(image)
        prob_map = self._det.run(None, {self._det_input: img})[0][0, 0]
        boxes = self._db_postprocess(prob_map)
        result = []
        for box in boxes:
            # bitmap → 原图坐标（ratio = bitmap/src）
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
        batch = self._rec_preprocess(crop)
        probs = self._rec.run(None, {self._rec_input: batch})[0]  # (1, T, C)
        return self._ctc_decode(probs[0])

    # ---------- det 预/后处理（最小 DB 实现） ----------

    def _det_preprocess(self, image: np.ndarray) -> tuple[np.ndarray, float, float]:
        """返回 (blob, ratio_h, ratio_w)，ratio = 模型输入边 / 原图边。

        静态输入（zbox 分发的 .onnx 为 480x480，与板上 .rknn 一致）：整图拉伸到
        固定尺寸；动态输入（DetResizeForTest limit_type=max）：长边 480、32 倍对齐。
        """
        h, w = image.shape[:2]
        if self._det_size is not None:
            rh, rw = self._det_size
        else:
            ratio = min(1.0, self.DET_LIMIT_SIDE_LEN / max(h, w))
            rh = max(32, int(round(h * ratio / 32.0)) * 32)
            rw = max(32, int(round(w * ratio / 32.0)) * 32)
        resized = cv2.resize(image, (rw, rh)).astype(np.float32) / 255.0
        resized = (resized - self.DET_MEAN) / self.DET_STD
        blob = resized.transpose(2, 0, 1)[np.newaxis].astype(np.float32)
        return np.ascontiguousarray(blob), rh / h, rw / w

    def _db_postprocess(self, pred: np.ndarray) -> list[np.ndarray]:
        """DB 最小后处理：阈值化 → 连通域 → 最小外接矩形 → unclip → 源图坐标。"""
        bitmap = (pred > self.DET_THRESH).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[np.ndarray] = []
        for contour in contours[:1000]:
            points, sside = _mini_boxes(contour)
            if sside < 3:
                continue
            score = _box_score_fast(pred, points.reshape(-1, 2))
            if score < self.DET_BOX_THRESH:
                continue
            unclipped = _unclip(points, self.DET_UNCLIP_RATIO)
            if not len(unclipped):
                continue
            box, sside = _mini_boxes(unclipped)
            if sside < 5:
                continue
            boxes.append(_order_points_clockwise(box))
        return boxes

    # ---------- rec 预/后处理 ----------

    @staticmethod
    def _load_characters(dict_path: Path) -> list[str]:
        """ppocr_keys_v1 语义：['blank'] + 字典行（首行可为空行）+ [' ']，与官方 CTCLabelDecode 一致。"""
        chars = []
        with dict_path.open("rb") as fh:
            for raw in fh.readlines():
                chars.append(raw.decode("utf-8").strip("\n").strip("\r\n"))
        chars.append(" ")
        return ["blank"] + chars

    def _rec_preprocess(self, crop: np.ndarray) -> np.ndarray:
        h, w = crop.shape[:2]
        ratio = self.REC_IMAGE_HEIGHT / h
        rw = min(int(np.ceil(w * ratio)), self.REC_MAX_WIDTH)
        resized = cv2.resize(crop, (rw, self.REC_IMAGE_HEIGHT))
        resized = (resized.astype(np.float32) / 255.0 - 0.5) / 0.5
        padded = np.full(
            (self.REC_IMAGE_HEIGHT, self.REC_MAX_WIDTH, 3), -1.0, dtype=np.float32
        )
        padded[:, :rw] = resized
        return padded.transpose(2, 0, 1)[np.newaxis].astype(np.float32)

    def _ctc_decode(self, probs: np.ndarray) -> str:
        """贪心 CTC：blank=0 折叠重复；index 0='blank'，末位=' '。"""
        indices = probs.argmax(axis=-1)
        chars: list[str] = []
        prev = -1
        for idx in indices:
            if idx != prev and idx != 0:
                chars.append(self._characters[idx])
            prev = idx
        return "".join(chars)


# ---------- DB 后处理工具（与 rknn_model_zoo db_postprocess 等价的最小实现） ----------


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
