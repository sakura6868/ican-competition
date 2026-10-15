"""资产处理流水线测试：上传→入队→worker→转码/抽帧产物。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from gongxu.assets import service
from gongxu.config import get_settings
from gongxu.jobs import service as jobs
from gongxu.media.processor import ProcessError, run_asset_job


@pytest.fixture()
def sample_video(tmp_path_factory) -> Path:
    from gongxu.media.video import FFmpegNotFoundError, _ffmpeg_bin

    try:
        ffmpeg = _ffmpeg_bin()
    except FFmpegNotFoundError:
        pytest.skip("无 ffmpeg")
    video = tmp_path_factory.mktemp("vid") / "op.mp4"
    proc = __import__("subprocess").run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=160x120:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip("无法生成测试视频")
    return video


def test_asset_process_end_to_end(db: Session, sample_video: Path) -> None:
    asset = service.register_asset(
        db,
        kind="video",
        filename="op.mp4",
        temp_path=sample_video,
        license_note="test",
        sha256=None,
    )
    job = jobs.enqueue(
        db,
        job_type="asset_process",
        payload={"asset_id": asset.id},
        idempotency_key=f"process:{asset.id}",
    )
    claimed = jobs.claim_next(db, worker_name="test-worker", job_types=["asset_process"])
    assert claimed is not None and claimed.id == job.id

    result = run_asset_job(db, job.id)
    assert result["kind"] == "video"
    assert result["duration_s"] > 0
    assert result["transcoded_id"]
    assert result["keyframe_count"] >= 1
    assert len(result["keyframe_ids"]) == result["keyframe_count"]
    # 衍生物已加密落库：密文存在且无明文代理/关键帧残留（整改 R3）
    from gongxu.assets.derived import decrypt_derived_to_temp
    from gongxu.config import get_settings

    temp = Path(get_settings().data_root) / "runtime" / "verify"
    out = decrypt_derived_to_temp(db, result["transcoded_id"], temp_dir=temp)
    assert out.exists() and out.stat().st_size > 0

    done = db.get(__import__("gongxu.db.models", fromlist=["Job"]).Job, job.id)
    assert done.status.value == "succeeded"
    assert done.checkpoint["stage"] == "done"


def test_asset_process_missing_asset(db: Session) -> None:
    job = jobs.enqueue(
        db,
        job_type="asset_process",
        payload={"asset_id": "nonexistent"},
        idempotency_key="process:nonexistent",
    )
    with pytest.raises(ProcessError):
        run_asset_job(db, job.id)


def test_non_video_asset_indexed_as_document(db: Session) -> None:
    """手册资产入队处理 → 切片入库供 RAG。"""
    from pathlib import Path as P

    tmp = P(get_settings().data_root) / "runtime" / "t.txt"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("安全规范：" + "断电后拔插头。" * 200, encoding="utf-8")
    asset = service.register_asset(
        db,
        kind="manual",
        filename="t.txt",
        temp_path=tmp,
        license_note="",
        sha256=None,
    )
    job = jobs.enqueue(
        db,
        job_type="asset_process",
        payload={"asset_id": asset.id},
        idempotency_key=f"process:{asset.id}",
    )
    result = run_asset_job(db, job.id)
    assert result.get("indexed_chunks", 0) >= 1
    assert result["kind"] == "manual"
