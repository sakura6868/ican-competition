"""视觉工具脚本测试：onnx 后处理辅助、compare 评估、fetch_models 登记。

不依赖真实模型/网络：onnx 辅助为纯函数；compare/fetch_models 用 monkeypatch
注入适配器与下载器。
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.vision.adapters import VisionAdapter
from gongxu.vision.facts import Detection


def _png_bytes(size: tuple[int, int] = (64, 48)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


class FakeAdapter(VisionAdapter):
    name = "fake"

    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        return [
            Detection(
                timestamp_ms=timestamp_ms,
                hand="right",
                tool="wrench",
                object="locking_nut",
                action="loosen",
                relation="right_holds_wrench",
                confidence=0.9,
                frame_hash=f"fake_{timestamp_ms}",
            )
        ]

    def model_version(self) -> str:
        return "fake@v1"


# ---------- adapters_onnx 纯函数 ----------


def test_letterbox_pads_and_scales() -> None:
    from gongxu.vision.adapters_onnx import _letterbox

    rgb = np.zeros((500, 1000, 3), dtype=np.uint8)  # 大图 → 下采样
    canvas, r, x0, y0 = _letterbox(rgb, size=640)
    assert canvas.shape == (640, 640, 3)
    assert 0 < r <= 1.0
    assert 0 <= x0 < 640 and 0 <= y0 < 640
    assert round(1000 * r) + x0 <= 640
    assert round(500 * r) + y0 <= 640


def test_nms_keeps_non_overlapping_drops_overlap() -> None:
    from gongxu.vision.adapters_onnx import _nms

    boxes = np.array(
        [
            [10, 10, 60, 60],     # 高分
            [20, 20, 70, 70],     # 与前者高重叠 → 丢弃
            [200, 200, 260, 260],  # 独立 → 保留
        ],
        dtype=np.float32,
    )
    scores = np.array([0.9, 0.8, 0.85], dtype=np.float32)
    keep = _nms(boxes, scores, iou_thres=0.45)
    assert keep == [0, 2]


def test_box_iou_overlap_and_disjoint() -> None:
    from gongxu.vision.adapters_onnx import _box_iou

    a = (0.0, 0.0, 100.0, 100.0)
    assert _box_iou(a, (50.0, 50.0, 100.0, 100.0)) > 0.0
    assert _box_iou(a, (200.0, 200.0, 300.0, 300.0)) == 0.0


def test_coco_names_length() -> None:
    from gongxu.vision.adapters_onnx import _COCO_NAMES

    assert len(_COCO_NAMES) == 80


# ---------- compare 评估脚本 ----------


def _write_frames(tmp_path: Path, count: int = 3) -> list[Path]:
    frames = []
    for i in range(count):
        p = tmp_path / f"kf_{i:04d}.jpg"
        p.write_bytes(_png_bytes())
        frames.append(p)
    return frames


def test_compare_det_to_dict() -> None:
    from gongxu.vision.compare import _det_to_dict

    d = Detection(
        timestamp_ms=1000, hand="left", tool="wrench", object="locking_nut",
        action="loosen", relation="holds", confidence=0.9, frame_hash="abc",
    )
    out = _det_to_dict(d)
    assert out["hand"] == "left" and out["tool"] == "wrench"
    assert out["confidence"] == 0.9 and out["timestamp_ms"] == 1000


def test_compare_run_backend_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gongxu.vision import compare

    frames = _write_frames(tmp_path)
    monkeypatch.setattr(compare, "_make_adapter", lambda backend: FakeAdapter())
    result = compare._run_backend("onnx", frames)
    assert result["ok"] is True
    assert result["total_detections"] == len(frames)
    assert result["observations"] >= 1
    assert result["matched_steps"] >= 1
    assert result["model_version"] == "fake@v1"
    assert result["latency_ms_avg"] >= 0.0


def test_compare_run_backend_adapter_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.vision import compare
    from gongxu.vision.adapters import AdapterUnavailableError

    class FailAdapter(VisionAdapter):
        name = "fail"

        def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
            raise AdapterUnavailableError("模拟失败")

    frames = _write_frames(tmp_path)
    monkeypatch.setattr(compare, "_make_adapter", lambda backend: FailAdapter())
    result = compare._run_backend("qwen", frames)
    assert result["ok"] is False
    assert "模拟失败" in result["error"]


def test_compare_print_summary(capsys: pytest.CaptureFixture[str]) -> None:
    from gongxu.vision.compare import _print_summary

    report = {
        "backends": {
            "onnx": {
                "ok": True, "model_version": "onnx@v1", "latency_ms_avg": 120.5,
                "total_detections": 10, "observations": 4,
                "steps": [{"judgement": "passed"}, {"judgement": "passed"}],
                "quality_reason": "",
            },
            "qwen": {"ok": False, "error": "缺少配置"},
        }
    }
    _print_summary(report)
    out = capsys.readouterr().out
    assert "onnx" in out and "通过步骤=2/2" in out
    assert "qwen" in out and "失败" in out


# ---------- fetch_models 脚本 ----------


def test_fetch_download_skips_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gongxu.vision import fetch_models

    dest = tmp_path / "m.bin"
    dest.write_bytes(b"data")

    def _fail(*a, **k):
        pytest.fail("文件已存在不应发起下载")

    monkeypatch.setattr("httpx.stream", _fail)
    assert fetch_models._download("http://example.invalid/x", dest, force=False) is False


def test_fetch_download_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gongxu.vision import fetch_models

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            yield b"chunk1"
            yield b"chunk2"

    class _Stream:
        def __enter__(self) -> _Resp:
            return _Resp()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("httpx.stream", lambda *a, **k: _Stream())
    dest = tmp_path / "m.bin"
    assert fetch_models._download("http://example.invalid/x", dest, force=True) is True
    assert dest.read_bytes() == b"chunk1chunk2"


def test_cmd_fetch_models_registers(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.vision import fetch_models
    from gongxu.vision.models import get_model

    monkeypatch.setenv("GONGXU_MODEL_ROOT", str(tmp_path))
    get_settings.cache_clear()
    (tmp_path / "hand_landmarker.task").write_bytes(b"hand")
    (tmp_path / "yolov8n.onnx").write_bytes(b"yolo")
    try:
        code = fetch_models.cmd_fetch_models(argparse.Namespace(force=False))
        assert code == 0
        hand = get_model(db, "hand")
        tool = get_model(db, "tool_detector")
        assert hand is not None and tool is not None
        assert hand.status.value == "available"
        assert tool.path == str(tmp_path / "yolov8n.onnx")
    finally:
        get_settings.cache_clear()
