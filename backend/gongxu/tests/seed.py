"""E2E 测试种子：空库迁移 + 创建测试账号与最小资产（整改 P1-5）。

供 CI/本地 Playwright 前置使用：cd backend && uv run python -m gongxu.tests.seed
"""

from __future__ import annotations

import subprocess  # nosec B404 -- 仅调用受信任的 imageio-ffmpeg 可执行文件
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.db.models import SopPublishStatus, SopVersion, SourceAsset, User, UserRole
from gongxu.db.session import get_session_factory, init_db
from gongxu.providers.replay import ReplayProvider
from gongxu.security import auth
from gongxu.sop import service as sop_service
from gongxu.sop.fixtures import PROCESS_KEY


def _get_db() -> Session:
    init_db()
    return get_session_factory()()


E2E_USERNAME = "e2e_instructor"
E2E_PASSWORD = "correct-horse-42"  # nosec B105: local E2E fixture only
E2E_REFERENCE_FILENAME = "e2e-reference-fixture.mp4"


def ensure_e2e_user(db: Session) -> User:
    """Create or restore the dedicated local E2E instructor deterministically."""
    user = db.scalar(select(User).where(User.username == E2E_USERNAME))
    if user is None:
        return auth.create_user(
            db,
            username=E2E_USERNAME,
            password=E2E_PASSWORD,
            role=UserRole.instructor,
            display_name="E2E 教师",
        )

    user.password_hash = auth.hash_password(E2E_PASSWORD)
    user.role = UserRole.instructor
    user.display_name = "E2E 教师"
    user.is_active = True
    db.commit()
    db.refresh(user)
    return user


def ensure_e2e_published_sop(db: Session, user: User) -> SopVersion:
    """建立真实加密来源资产和已发布 fixture SOP，供全栈拒判流程使用。"""
    existing = db.scalar(
        select(SopVersion)
        .where(
            SopVersion.process_key == PROCESS_KEY,
            SopVersion.status == SopPublishStatus.published,
        )
        .order_by(SopVersion.version.desc())
    )
    existing_asset = (
        db.get(SourceAsset, existing.reference_asset_id)
        if existing is not None and existing.reference_asset_id
        else None
    )
    if (
        existing is not None
        and existing_asset is not None
        and existing_asset.encrypted_path
        and Path(existing_asset.encrypted_path).is_file()
    ):
        return existing

    from gongxu.assets import service as asset_service
    from gongxu.media.video import _ffmpeg_bin

    runtime = get_settings().data_root / "runtime" / "e2e-seed"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reference_", dir=runtime) as tmpdir:
        source = Path(tmpdir) / E2E_REFERENCE_FILENAME
        process = subprocess.run(  # noqa: S603  # nosec B603 -- 固定 ffmpeg 参数
            [
                _ffmpeg_bin(),
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.6:r=5",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            capture_output=True,
            text=True,
        )
        if process.returncode != 0 or not source.is_file():
            raise RuntimeError(f"E2E 参考视频生成失败: {process.stderr.strip()}")
        asset = asset_service.register_asset(
            db,
            kind="video",
            filename=E2E_REFERENCE_FILENAME,
            temp_path=source,
            license_note="自动化测试生成的非真人色视频",
            authorized_by=user.id,
            allow_provider_export=True,
        )

    draft = sop_service.compile_sop(
        db,
        provider=ReplayProvider(),
        process_key=PROCESS_KEY,
        analysis={"source": "e2e_fixture_seed"},
        rag_chunks=[],
        compiled_by=user.id,
    )
    draft.reference_asset_id = asset.id
    draft.source_asset_ids = [asset.id]
    db.commit()
    return sop_service.publish_sop(db, draft.id, published_by=user.id)


def seed_e2e() -> int:
    """建立账号和全栈拒判测试所需的已发布 fixture SOP。"""
    db = _get_db()
    try:
        user = ensure_e2e_user(db)
        sop = ensure_e2e_published_sop(db, user)
        print(f"seed ok: {E2E_USERNAME} 与 SOP {sop.id} 已就绪")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(seed_e2e())
