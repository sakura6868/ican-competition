"""端到端演示脚本测试：_dhash/_dedupe_frames 纯逻辑 + cmd_demo 链路。

cmd_demo 用 monkeypatch 隔离解密/抽帧/qwen 适配器，不依赖真实模型与网络。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.vision import demo
from gongxu.vision.adapters import VisionAdapter
from gongxu.vision.facts import Detection


def _png_bytes(size: tuple[int, int] = (64, 48), color: int = 128) -> bytes:
    """随机纹理图（color 作种子），保证 dHash 可区分。"""
    import io

    import numpy as np

    rng = np.random.default_rng(color)
    arr = (rng.random((size[1], size[0], 3)) * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _write_frames(tmp_path: Path, colors: list[int]) -> list[Path]:
    frames = []
    for i, c in enumerate(colors):
        p = tmp_path / f"kf_{i:04d}.jpg"
        p.write_bytes(_png_bytes(color=c))
        frames.append(p)
    return frames


def test_dhash_differs_by_content() -> None:
    dark = demo._dhash(_png_bytes(color=10))
    light = demo._dhash(_png_bytes(color=245))
    same = demo._dhash(_png_bytes(color=10))
    assert dark != light
    assert dark == same


def test_dedupe_keeps_distinct_keeps_floor(tmp_path: Path) -> None:
    # 颜色交替变化 → 全部保留
    frames = _write_frames(tmp_path, [10, 200, 30, 210, 50, 220])
    kept = demo._dedupe_frames(frames, hamming=2, min_keep=8)
    assert len(kept) >= 6  # 全部差异明显
    # 全同色 → 去重后不足 min_keep → 均匀补帧到 min_keep
    sub = tmp_path / "b"
    sub.mkdir()
    frames2 = _write_frames(sub, [10] * 12)
    kept2 = demo._dedupe_frames(frames2, hamming=2, min_keep=8)
    assert len(kept2) == 8


class _FakeQwen(VisionAdapter):
    name = "qwen-fake"

    def __init__(self, *, process_key: str = "") -> None:
        self.process_key = process_key

    def infer_batch(self, frames: list[tuple[bytes, int]]) -> list[list[Detection]]:
        out: list[list[Detection]] = []
        for _img, ts in frames:
            out.append(
                [
                    Detection(
                        timestamp_ms=ts, hand="both", object="gearbox_housing",
                        action="hold", relation="both_operates_gearbox_housing",
                        confidence=0.85, frame_hash="demo",
                    )
                ]
            )
        return out

    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        return [
            Detection(
                timestamp_ms=timestamp_ms, hand="both", object="gearbox_housing",
                action="hold", relation="both_operates_gearbox_housing",
                confidence=0.85, frame_hash="demo",
            )
        ]

    def model_version(self) -> str:
        return "qwen@fake"


def test_cmd_demo_effective_scoring(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cmd_demo 全链路：真实资产 → 假 qwen → 有效评分（step2 passed）。"""
    from gongxu.assets import service as asset_service

    fake_video = Path(get_settings().data_root) / "runtime" / "demo.mp4"
    fake_video.parent.mkdir(parents=True, exist_ok=True)
    fake_video.write_bytes(b"\x00" * 100)
    asset = asset_service.register_asset(
        db, kind="video", filename="demo.mp4", temp_path=fake_video, license_note="test"
    )

    def _fake_extract(video_path: Path, frames_dir: Path, **kwargs: object) -> None:
        frames_dir.mkdir(parents=True, exist_ok=True)
        colors = [10, 200, 30, 210, 50, 220, 70, 230]
        for i, c in enumerate(colors):
            (frames_dir / f"kf_{i:04d}.jpg").write_bytes(_png_bytes(color=c))

    monkeypatch.setattr(demo.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(demo, "extract_keyframes_uniform", _fake_extract)
    monkeypatch.setattr(demo, "QwenVisionAdapter", _FakeQwen)
    # mock probe：均匀采样需读视频时长算时间戳
    from gongxu.media.video import VideoInfo

    monkeypatch.setattr(
        demo,
        "probe",
        lambda _p: VideoInfo(
            duration_s=8.0, width=640, height=480, fps=30.0, codec="h264"
        ),
    )
    # 报告写入临时目录，避免污染真实 reports
    demo._REPORT = tmp_path / "report-out"
    demo._REPORT.mkdir(parents=True, exist_ok=True)

    args = argparse.Namespace(
        asset=asset.id, process="impact_gearbox_disassembly_demo"
    )
    code = demo.cmd_demo(args)
    assert code == 0

    import json

    report = json.loads(
        (tmp_path / "report-out" / f"demo-assessment-{asset.id[:8]}.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["scoring"]["result"] in ("pass", "fail", "inconclusive")
    judgements = {s["index"]: s["judgement"] for s in report["steps"]}
    # 每帧都报 gearbox_housing+hold → step2 passed
    assert judgements[2] == "passed"


def test_cmd_demo_unknown_process_rejects(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from gongxu.assets import service as asset_service

    fake_video = Path(get_settings().data_root) / "runtime" / "demo.mp4"
    fake_video.parent.mkdir(parents=True, exist_ok=True)
    fake_video.write_bytes(b"\x00" * 100)
    asset = asset_service.register_asset(
        db, kind="video", filename="demo.mp4", temp_path=fake_video, license_note="test"
    )
    monkeypatch.setattr(demo.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(demo, "QwenVisionAdapter", _FakeQwen)

    args = argparse.Namespace(asset=asset.id, process="no_such_process")
    assert demo.cmd_demo(args) == 1
