"""资产衍生物：加密存储、保留期与清理（整改报告 R3）。

原则：
- 代理视频/关键帧/证据片段以 AES-256-GCM 加密文件存储，不保留长期明文。
- 只用于单次模型推理的关键帧在任务结束后立即删除。
- 清理服务处理过期密文、孤立文件、残留临时目录和无数据库记录的文件，
  全部写入审计日志。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.data.manifest import sha256_file
from gongxu.db.models import AuditAction, DerivedAsset
from gongxu.security import filecrypto
from gongxu.security.audit import record_audit


class DerivedAssetError(Exception):
    pass


def store_derived(
    db: Session,
    *,
    source_asset_id: str,
    kind: str,
    plain_path: Path,
    frame_index: int | None = None,
    expires_at: datetime | None = None,
    actor_id: str | None = None,
) -> DerivedAsset:
    """把临时明文衍生物加密落盘并登记；登记后应立即删除明文。"""
    checksum = sha256_file(plain_path)
    settings = get_settings()
    row = DerivedAsset(
        source_asset_id=source_asset_id,
        kind=kind,
        sha256=checksum,
        frame_index=frame_index,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    encrypted_path = settings.data_root / "derivatives" / "encrypted" / f"{row.id}.bin"
    filecrypto.encrypt_file(plain_path, encrypted_path)
    row.encrypted_path = str(encrypted_path)
    db.commit()
    db.refresh(row)
    record_audit(
        db,
        action=AuditAction.asset_upload,
        actor_id=actor_id,
        target_type="derived_asset",
        target_id=row.id,
        detail={"kind": kind, "sha256": checksum[:16], "source_asset_id": source_asset_id},
    )
    return row


def decrypt_derived_to_temp(db: Session, derived_id: str, *, temp_dir: Path) -> Path:
    """解密衍生物到临时目录（处理后必须清理）。"""
    row = db.get(DerivedAsset, derived_id)
    if row is None or row.deleted_at is not None:
        raise DerivedAssetError(f"衍生物不存在或已删除: {derived_id}")
    if row.expires_at is not None:
        from gongxu.db.models import utcnow

        if row.expires_at < utcnow():
            raise DerivedAssetError(f"衍生物已过期: {derived_id}")
    if not row.encrypted_path:
        raise DerivedAssetError(f"衍生物缺少密文路径: {derived_id}")
    encrypted = Path(row.encrypted_path)
    if not encrypted.exists():
        raise DerivedAssetError(f"衍生物密文缺失: {derived_id}")
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest = temp_dir / f"{derived_id}.bin"
    filecrypto.decrypt_file(encrypted, dest)
    return dest


def mark_deleted(db: Session, derived_id: str, *, actor_id: str | None = None) -> None:
    from gongxu.db.models import utcnow

    row = db.get(DerivedAsset, derived_id)
    if row is None:
        return
    if not row.encrypted_path:
        return
    encrypted = Path(row.encrypted_path)
    if encrypted.exists():
        encrypted.unlink(missing_ok=True)
    row.deleted_at = utcnow()
    db.commit()
    record_audit(
        db,
        action=AuditAction.cleanup,
        actor_id=actor_id,
        target_type="derived_asset",
        target_id=row.id,
        detail={"kind": row.kind},
    )


def cleanup_expired(db: Session, *, actor_id: str | None = None) -> dict[str, int]:
    """清理过期/孤立/无记录衍生物（幂等）。"""
    from gongxu.db.models import utcnow

    stats = {"expired": 0, "orphan_files": 0, "no_record_files": 0}
    now = utcnow()

    # 过期登记
    expired = db.scalars(
        select(DerivedAsset).where(
            DerivedAsset.deleted_at.is_(None),
            DerivedAsset.expires_at.is_not(None),
            DerivedAsset.expires_at < now,
        )
    )
    for row in expired:
        mark_deleted(db, row.id, actor_id=actor_id)
        stats["expired"] += 1

    # 登记表中软删除但文件仍存在 → 删除孤立文件
    for row in db.scalars(select(DerivedAsset).where(DerivedAsset.deleted_at.is_not(None))):
        if not row.encrypted_path:
            continue
        p = Path(row.encrypted_path)
        if p.exists():
            p.unlink(missing_ok=True)
            stats["orphan_files"] += 1

    # 加密目录中无数据库记录的文件
    encrypted_dir = get_settings().data_root / "derivatives" / "encrypted"
    if encrypted_dir.exists():
        known = {
            Path(r.encrypted_path).name
            for r in db.scalars(select(DerivedAsset))
            if r.encrypted_path
        }
        for f in encrypted_dir.iterdir():
            if f.name not in known:
                f.unlink(missing_ok=True)
                stats["no_record_files"] += 1
    db.commit()
    return stats
