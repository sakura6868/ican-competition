"""A 路径：本地 ONNX 视觉适配器（MediaPipe 手部 + YOLOv8n 物体检测）。

- 手部：mediapipe HandLandmarker（官方 hand_landmarker.task）→ 左右手 + 置信度。
- 物体：YOLOv8n（COCO 80 类）→ person（操作者在场）+ 疑似工具/零件框。
- 关系：手框与物体框交叠 → operates/holds。词表内工具 wrench/screwdriver/
  grinding_wheel 在 COCO 中不存在，诚实留空；B 路径微调后替换权重 + 类别映射即可。

模型缺失/加载失败 → AdapterUnavailableError（fail-closed，不产生伪事实）。
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import numpy as np

from gongxu.config import get_settings
from gongxu.vision.adapters import AdapterUnavailableError, VisionAdapter
from gongxu.vision.facts import Detection, hash_frame

# YOLOv8 COCO 80 类（官方顺序）。
_COCO_NAMES: list[str] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]
# COCO 中与「工具/零件」语义接近的类别：检出时如实上报（不在工序词表，不进评分对齐）。
_TOOLISH = {"scissors", "knife", "fork", "spoon", "cup", "bottle"}
_CONF_THRESHOLD = 0.30
_NMS_IOU = 0.45
_HAND_OBJECT_IOU = 0.10
_POSTPROCESS_CONTRACT_VERSION = "post-v1"
_WEIGHT_DIGEST_LENGTH = 12

Box = tuple[float, float, float, float]


def _weight_sha256(path: Path) -> str:
    """Return a short content fingerprint without mutating or loading the model."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AdapterUnavailableError(f"无法读取 ONNX 视觉模型 {path.name}: {exc}") from exc
    return digest.hexdigest()[:_WEIGHT_DIGEST_LENGTH]


def _letterbox(rgb: np.ndarray, size: int = 640) -> tuple[np.ndarray, float, int, int]:
    """保持宽高比的 letterbox 填充，返回 (画布, 缩放比, pad_x, pad_y)。"""
    from PIL import Image

    h, w = rgb.shape[:2]
    r = min(size / w, size / h)
    nw, nh = round(w * r), round(h * r)
    x0, y0 = (size - nw) // 2, (size - nh) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    resized = Image.fromarray(rgb).resize((nw, nh), Image.Resampling.BILINEAR)
    canvas[y0 : y0 + nh, x0 : x0 + nw] = np.asarray(resized, dtype=np.uint8)
    return canvas, r, x0, y0


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float) -> list[int]:
    """简单 NMS（框数量少，Python 循环足够）。boxes: (N,4) xyxy 原图坐标。"""
    order = np.argsort(scores)[::-1].tolist()
    keep: list[int] = []
    while order:
        idx = order.pop(0)
        keep.append(idx)
        if not order:
            break
        box = boxes[idx]
        others = boxes[order]
        x1 = np.maximum(box[0], others[:, 0])
        y1 = np.maximum(box[1], others[:, 1])
        x2 = np.minimum(box[2], others[:, 2])
        y2 = np.minimum(box[3], others[:, 3])
        inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        area_a = (box[2] - box[0]) * (box[3] - box[1])
        area_b = (others[:, 2] - others[:, 0]) * (others[:, 3] - others[:, 1])
        union = area_a + area_b - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = [o for i, o in enumerate(order) if iou[i] <= iou_thres]
    return keep


def _box_iou(a: Box, b: Box) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    x1, y1 = max(ax1, bx1), max(ay1, by1)
    x2, y2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class OnnxVisionAdapter(VisionAdapter):
    """本地 ONNX 推理：MediaPipe 手部 + YOLOv8n 物体。模型缺失即拒判。"""

    name = "onnx"

    # 类级懒加载：模型权重在进程内只加载一次，避免每帧重建推理会话
    _landmarker: Any | None = None
    _yolo: Any | None = None

    def __init__(self) -> None:
        root = get_settings().model_root
        hand_model = root / "hand_landmarker.task"
        yolo_model = root / "yolov8n.onnx"
        missing = [p.name for p in (hand_model, yolo_model) if not p.exists()]
        if missing:
            raise AdapterUnavailableError(
                f"ONNX 视觉模型缺失: {', '.join(missing)}；"
                "请运行 python -m gongxu.vision.fetch_models"
            )
        self._hand_model = hand_model
        self._yolo_model = yolo_model
        self._hand_model_sha = _weight_sha256(hand_model)
        self._yolo_model_sha = _weight_sha256(yolo_model)

    @property
    def landmarker(self) -> Any:
        if OnnxVisionAdapter._landmarker is None:
            OnnxVisionAdapter._landmarker = self._load_hand(str(self._hand_model))
        return OnnxVisionAdapter._landmarker

    @property
    def yolo(self) -> Any:
        if OnnxVisionAdapter._yolo is None:
            OnnxVisionAdapter._yolo = self._load_yolo(str(self._yolo_model))
        return OnnxVisionAdapter._yolo

    @staticmethod
    def _load_hand(model_path: str) -> Any:
        from mediapipe.tasks import python as mp_python  # type: ignore[import-untyped]
        from mediapipe.tasks.python import vision as mp_vision  # type: ignore[import-untyped]

        base = mp_python.BaseOptions(model_asset_path=model_path)
        opts = mp_vision.HandLandmarkerOptions(
            base_options=base,
            num_hands=2,
            min_hand_detection_confidence=0.5,
        )
        return mp_vision.HandLandmarker.create_from_options(opts)

    @staticmethod
    def _load_yolo(model_path: str) -> Any:
        import onnxruntime as ort  # type: ignore[import-untyped]

        return ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])

    def model_version(self) -> str:
        return (
            f"onnx@h-{self._hand_model_sha}.y-{self._yolo_model_sha}."
            f"{_POSTPROCESS_CONTRACT_VERSION}"
        )

    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        try:
            from PIL import Image

            rgb = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"), dtype=np.uint8)
        except Exception as exc:
            raise AdapterUnavailableError(f"图像解码失败: {exc}") from exc
        fh = hash_frame(image_bytes)

        hand_dets = self._detect_hands(rgb, timestamp_ms=timestamp_ms, frame_hash=fh)
        obj_boxes = self._detect_objects(rgb)

        detections: list[Detection] = []
        for det, hand_box in hand_dets:
            for cls_name, box in obj_boxes:
                if cls_name == "person":
                    continue
                if _box_iou(hand_box, box) >= _HAND_OBJECT_IOU:
                    det.object = cls_name
                    det.relation = "operates"
                    break
            detections.append(det)

        # person → 操作者在场（上下文证据，非评分对齐要素）
        for cls_name, _box in obj_boxes:
            if cls_name == "person":
                detections.append(
                    Detection(
                        timestamp_ms=timestamp_ms,
                        object="operator",
                        relation="operator_present",
                        confidence=0.9,
                        frame_hash=fh,
                    )
                )
        return detections

    def _detect_hands(
        self, rgb: np.ndarray, *, timestamp_ms: int, frame_hash: str
    ) -> list[tuple[Detection, Box]]:
        import mediapipe as mp  # type: ignore[import-untyped]

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self.landmarker.detect(mp_image)
        h, w = rgb.shape[:2]
        dets: list[tuple[Detection, Box]] = []
        for handedness, landmarks in zip(result.handedness, result.hand_landmarks, strict=False):
            label = handedness[0].category_name if handedness else "Left"
            score = float(handedness[0].score) if handedness else 0.0
            xs = [lm.x * w for lm in landmarks]
            ys = [lm.y * h for lm in landmarks]
            box = (min(xs), min(ys), max(xs), max(ys))
            det = Detection(
                timestamp_ms=timestamp_ms,
                hand="left" if label == "Left" else "right",
                relation="hand_present",
                confidence=score,
                frame_hash=frame_hash,
            )
            dets.append((det, box))
        return dets

    def _detect_objects(self, rgb: np.ndarray) -> list[tuple[str, Box]]:
        """YOLOv8n 检测 → [(COCO 类名, xyxy 原图坐标)]，仅保留 person 与疑似工具类。"""
        canvas, r, x0, y0 = _letterbox(rgb, 640)
        blob = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
        out = self.yolo.run(None, {self.yolo.get_inputs()[0].name: blob})[0]
        preds = np.asarray(out, dtype=np.float32)[0].T  # (8400, 84)
        if preds.shape[0] == 0:
            return []
        boxes_xywh = preds[:, :4]
        class_scores = preds[:, 4:]
        class_ids = class_scores.argmax(axis=1)
        confs = class_scores.max(axis=1)
        mask = confs >= _CONF_THRESHOLD
        if not np.any(mask):
            return []
        sel_ids = class_ids[mask]
        sel_confs = confs[mask]
        sel_boxes = boxes_xywh[mask]
        cx = sel_boxes[:, 0]
        cy = sel_boxes[:, 1]
        bw = sel_boxes[:, 2]
        bh = sel_boxes[:, 3]
        x1 = ((cx - bw / 2) * 640 - x0) / r
        y1 = ((cy - bh / 2) * 640 - y0) / r
        x2 = ((cx + bw / 2) * 640 - x0) / r
        y2 = ((cy + bh / 2) * 640 - y0) / r
        xyxy = np.stack([x1, y1, x2, y2], axis=1)
        keep = _nms(xyxy, sel_confs, _NMS_IOU)
        results: list[tuple[str, Box]] = []
        for i in keep:
            cls_name = _COCO_NAMES[int(sel_ids[i])]
            if cls_name == "person" or cls_name in _TOOLISH:
                b = (float(xyxy[i, 0]), float(xyxy[i, 1]), float(xyxy[i, 2]), float(xyxy[i, 3]))
                results.append((cls_name, b))
        return results
