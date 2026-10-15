from __future__ import annotations

from pathlib import Path

from gongxu.db.models import SopPublishStatus, SourceAsset, UserRole
from gongxu.security import auth
from gongxu.tests.seed import (
    E2E_PASSWORD,
    E2E_USERNAME,
    ensure_e2e_published_sop,
    ensure_e2e_user,
)


def test_e2e_seed_restores_dedicated_account(db):
    user = ensure_e2e_user(db)
    user.password_hash = auth.hash_password("temporarily-changed")
    user.role = UserRole.learner
    user.display_name = "changed"
    user.is_active = False
    db.commit()

    restored = ensure_e2e_user(db)

    assert restored.username == E2E_USERNAME
    assert auth.verify_password(E2E_PASSWORD, restored.password_hash)
    assert restored.role == UserRole.instructor
    assert restored.display_name == "E2E 教师"
    assert restored.is_active is True


def test_e2e_seed_creates_idempotent_published_sop_with_encrypted_source(db):
    user = ensure_e2e_user(db)

    first = ensure_e2e_published_sop(db, user)
    second = ensure_e2e_published_sop(db, user)

    assert second.id == first.id
    assert first.status == SopPublishStatus.published
    reference = db.get(SourceAsset, first.reference_asset_id)
    assert reference is not None
    assert reference.encrypted_path is not None
    assert Path(reference.encrypted_path).is_file()
