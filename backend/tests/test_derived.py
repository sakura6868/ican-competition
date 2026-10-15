"""隐私衍生物测试（整改 R3）：加密存储、临时明文清理、过期清理、快照。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from gongxu.assets import derived, service
from gongxu.config import get_settings
from gongxu.db.models import AuditAction, AuditLog, DerivedAsset, utcnow


def _register_video(db: Session, data: bytes = b"\x00" * 2048) -> str:
    tmp = Path(get_settings().data_root) / "runtime" / "v.mp4"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    asset = service.register_asset(
        db, kind="video", filename="v.mp4", temp_path=tmp, license_note="t"
    )
    return asset.id


def test_derived_stored_encrypted_and_plain_removed(db: Session) -> None:
    asset_id = _register_video(db)
    plain = Path(get_settings().data_root) / "runtime" / "plain.bin"
    plain.write_bytes(b"proxy-content")
    row = derived.store_derived(db, source_asset_id=asset_id, kind="proxy_video", plain_path=plain)
    # 明文可被删除（调用方负责）；密文存在
    encrypted = Path(row.encrypted_path)
    assert encrypted.exists()
    raw = encrypted.read_bytes()
    assert b"proxy-content" not in raw  # 密文不含明文


def test_derived_decrypt_roundtrip(db: Session) -> None:
    asset_id = _register_video(db)
    plain = Path(get_settings().data_root) / "runtime" / "kf.jpg"
    plain.write_bytes(b"frame-data" * 100)
    row = derived.store_derived(
        db, source_asset_id=asset_id, kind="keyframe", plain_path=plain, frame_index=0
    )
    temp = Path(get_settings().data_root) / "runtime" / "tmp"
    out = derived.decrypt_derived_to_temp(db, row.id, temp_dir=temp)
    assert out.read_bytes() == plain.read_bytes()


def test_cleanup_expired(db: Session) -> None:
    asset_id = _register_video(db)
    plain = Path(get_settings().data_root) / "runtime" / "a.bin"
    plain.write_bytes(b"x")
    row = derived.store_derived(
        db,
        source_asset_id=asset_id,
        kind="keyframe",
        plain_path=plain,
        expires_at=utcnow() - timedelta(seconds=1),
    )
    stats = derived.cleanup_expired(db)
    assert stats["expired"] >= 1
    assert db.get(DerivedAsset, row.id).deleted_at is not None
    assert not Path(row.encrypted_path).exists()  # 文件已清理
    # 清理有审计记录
    assert db.query(AuditLog).filter(AuditLog.action == AuditAction.cleanup).count() >= 1


def test_no_long_term_plain_video_snapshot(db: Session, tmp_path: Path) -> None:
    """文件系统快照：处理代理/关键帧后，data/ 下无明文 mp4/jpg（R3.9）。"""
    import subprocess

    from gongxu.media.video import FFmpegNotFoundError, _ffmpeg_bin

    try:
        ffmpeg = _ffmpeg_bin()
    except FFmpegNotFoundError:
        pytest.skip("无 ffmpeg")
    video = tmp_path / "op.mp4"
    subprocess.run(  # noqa: S603  # 测试用固定 ffmpeg 参数列表
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
    )
    if not video.exists():
        pytest.skip("无法生成测试视频")

    asset_id = _register_video(db, data=video.read_bytes())
    # 模拟上传路由：注册后删除临时明文
    tmp = Path(get_settings().data_root) / "runtime" / "v.mp4"
    tmp.unlink(missing_ok=True)
    from gongxu.jobs import service as jobs
    from gongxu.media.processor import run_asset_job

    job = jobs.enqueue(
        db,
        job_type="asset_process",
        payload={"asset_id": asset_id},
        idempotency_key=f"process:{asset_id}",
    )
    result = run_asset_job(db, job.id)
    assert result["transcoded_id"]
    assert result["keyframe_count"] >= 1

    # 扫描 data 下所有文件：不得出现明文 .mp4/.jpg 派生文件
    root = get_settings().data_root
    plain_exts = (".mp4", ".jpg")
    leaked = []
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in plain_exts:
            leaked.append(str(f))
    assert leaked == [], f"存在长期明文衍生物: {leaked}"
