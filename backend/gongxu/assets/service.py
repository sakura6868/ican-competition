"""资产服务：上传登记、SHA256、加密存储与授权记录。

原始资产加密保存（AES-256-GCM 分块），不保存明文。公开 IMPACT 数据与
用户资产使用完全不同的存储根目录（data/raw vs data/assets）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.data.manifest import sha256_file
from gongxu.db.models import SourceAsset
from gongxu.security import filecrypto


class AssetError(Exception):
    pass


class AssetNotFoundError(AssetError):
    pass


class AssetTooLargeError(AssetError):
    pass


def register_asset(
    db: Session,
    *,
    kind: str,
    filename: str,
    temp_path: Path,
    license_note: str,
    authorized_by: str | None = None,
    sha256: str | None = None,
    allow_provider_export: bool = False,
) -> SourceAsset:
    """登记并加密保存上传的资产。temp_path 处理后应立即删除。"""
    if kind not in ("video", "manual", "image"):
        raise AssetError(f"不支持的资产类型: {kind}")
    checksum = sha256 or sha256_file(temp_path)
    settings = get_settings()
    asset = SourceAsset(
        kind=kind,
        filename=filename,
        sha256=checksum,
        license_note=license_note,
        authorized_by=authorized_by,
        allow_provider_export=allow_provider_export,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    encrypted_path = settings.data_root / "assets" / "encrypted" / f"{asset.id}.bin"
    filecrypto.encrypt_file(temp_path, encrypted_path)
    asset.encrypted_path = str(encrypted_path)
    db.commit()
    db.refresh(asset)
    return asset


async def register_uploaded_asset(
    db: Session,
    *,
    upload: UploadFile,
    kind: str,
    license_note: str,
    authorized_by: str | None,
    allow_provider_export: bool = False,
) -> SourceAsset:
    """Persist an UploadFile through the encrypted asset path with bounded memory use."""
    if not upload.filename:
        raise AssetError("缺少文件名")
    settings = get_settings()
    temp_dir = settings.data_root / "runtime" / "uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = Path(upload.filename).suffix[:16]
    temp_path = temp_dir / f"upload_{uuid.uuid4().hex}{safe_suffix}"
    try:
        size = 0
        with temp_path.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise AssetTooLargeError(
                        f"文件超过 {settings.max_upload_bytes // 1024 // 1024} MiB 限制"
                    )
                output.write(chunk)
        return register_asset(
            db,
            kind=kind,
            filename=upload.filename,
            temp_path=temp_path,
            license_note=license_note,
            authorized_by=authorized_by,
            allow_provider_export=allow_provider_export,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def decrypt_to_temp(db: Session, asset_id: str, *, temp_dir: Path) -> Path:
    """解密资产到临时文件（处理后必须删除）。"""
    asset = db.get(SourceAsset, asset_id)
    if asset is None or not asset.encrypted_path:
        raise AssetNotFoundError(asset_id)
    encrypted = Path(asset.encrypted_path)
    if not encrypted.exists():
        raise AssetNotFoundError(f"{asset_id} 的密文缺失")
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest = temp_dir / f"decrypted_{asset_id}{Path(asset.filename).suffix}"
    filecrypto.decrypt_file(encrypted, dest)
    return dest
