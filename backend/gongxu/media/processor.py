"""资产处理流水线：转码、抽帧、镜头切分。

可恢复：使用 job.checkpoint 记录阶段进度；异常退出后 worker 可继续未完成阶段。
临时明文处理完立即删除。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from gongxu.assets import service
from gongxu.config import get_settings
from gongxu.db.models import Job, SourceAsset
from gongxu.jobs import service as jobs
from gongxu.media.video import MediaError, extract_keyframes, probe, scene_boundaries, transcode


class ProcessError(Exception):
    pass


def _job_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def process_asset(
    db: Session,
    asset_id: str,
    *,
    work_dir: Path,
) -> dict[str, Any]:
    """处理单个资产并返回衍生产物清单。

    work_dir 为本次运行的临时工作区；调用方负责清理。
    返回 {transcoded_id, keyframe_ids, scenes, ...}。
    """
    asset = db.get(SourceAsset, asset_id)
    if asset is None:
        raise ProcessError(f"资产不存在: {asset_id}")

    tmp = work_dir / "plain"
    tmp.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"asset_id": asset_id, "kind": asset.kind}

    if asset.kind != "video":
        # 手册/图片资产：文档切片入库，供 RAG 检索
        from gongxu.assets.indexing import ChunkError, index_document_asset

        try:
            indexed = index_document_asset(db, asset_id, work_dir=work_dir)
        except ChunkError as exc:
            raise ProcessError(_job_error(exc)) from exc
        result["indexed_chunks"] = indexed["chunks"]
        result["indexed_chars"] = indexed["chars"]
        return result

    plain = service.decrypt_to_temp(db, asset_id, temp_dir=tmp)
    try:
        info = probe(plain)
        result["duration_s"] = info.duration_s
        result["resolution"] = f"{info.width}x{info.height}"
        result["fps"] = info.fps

        work = work_dir / "derived"
        work.mkdir(parents=True, exist_ok=True)
        from gongxu.assets.derived import DerivedAssetError, store_derived

        # 转码 → 加密落库（不保留明文）
        transcoded = work / "proxy.mp4"
        transcode(plain, transcoded, width=1280)
        try:
            row = store_derived(
                db, source_asset_id=asset_id, kind="proxy_video", plain_path=transcoded
            )
        except DerivedAssetError as exc:
            raise ProcessError(_job_error(exc)) from exc
        result["transcoded_id"] = row.id

        # 抽帧 → 每个关键帧单独加密登记
        frames_dir = work / "keyframes"
        keyframes = extract_keyframes(plain, frames_dir, fps=1.0, max_frames=120)
        keyframe_ids = []
        for idx, kf in enumerate(keyframes):
            row = store_derived(
                db,
                source_asset_id=asset_id,
                kind="keyframe",
                plain_path=kf,
                frame_index=idx,
            )
            keyframe_ids.append(row.id)
        result["keyframe_ids"] = keyframe_ids
        result["keyframe_count"] = len(keyframe_ids)

        scenes = scene_boundaries(plain, threshold=0.4)
        result["scenes"] = scenes
        result["scene_count"] = len(scenes)
        return result
    except MediaError as exc:
        raise ProcessError(_job_error(exc)) from exc


def run_asset_job(db: Session, job_id: str) -> dict[str, Any]:
    """worker 调用：执行入队的资产处理任务并更新状态/检查点。"""
    job = db.get(Job, job_id)
    if job is None:
        raise ProcessError(f"任务不存在: {job_id}")
    payload = job.payload or {}
    asset_id = payload.get("asset_id")
    if not asset_id:
        raise ProcessError("任务缺少 asset_id")

    settings = get_settings()
    work_root = settings.data_root / "runtime" / "work"
    work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{job.id}_", dir=work_root) as tmpdir:
        jobs.heartbeat(db, job_id)
        result = process_asset(db, asset_id, work_dir=Path(tmpdir))
        jobs.save_checkpoint(db, job_id, {"stage": "done", **result})
        jobs.complete(db, job_id, result=result)
    return result
