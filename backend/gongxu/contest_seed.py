"""Create a deterministic, resettable local contest classroom."""

from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed local ffmpeg invocation
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.assets.service import register_asset
from gongxu.config import PROJECT_ROOT, get_settings
from gongxu.db.models import (
    PracticeObservation,
    PracticeSession,
    ProviderReceipt,
    SopPublishStatus,
    SopVersion,
    User,
    UserRole,
    utcnow,
)
from gongxu.db.session import get_session_factory, init_db
from gongxu.media.video import _ffmpeg_bin
from gongxu.practice.state_machine import initial_state
from gongxu.security import auth
from gongxu.sop.schema import SopStep

DEMO_PASSWORD = "ican-demo-2026"  # nosec B105 - resettable local contest fixture only


def _ensure_user(db: Session, username: str, role: UserRole, display_name: str) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        return auth.create_user(
            db,
            username=username,
            password=DEMO_PASSWORD,
            role=role,
            display_name=display_name,
        )
    user.password_hash = auth.hash_password(DEMO_PASSWORD)
    user.role = role
    user.display_name = display_name
    user.is_active = True
    db.commit()
    return user


def _source_video() -> tuple[Path, str, bool]:
    shoe = PROJECT_ROOT / "samples" / "public-videos" / "shoe-lacing" / "shoe-lacing.webm"
    if shoe.is_file():
        return (
            shoe,
            "Shoe Lacing Methods, Lrcolette, CC BY-SA 3.0, Wikimedia Commons",
            False,
        )
    runtime = get_settings().data_root / "runtime" / "contest-seed"
    runtime.mkdir(parents=True, exist_ok=True)
    fallback = runtime / "engineering-backup.example.mp4"
    if not fallback.is_file():
        command = [
            _ffmpeg_bin(),
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x6f64c5:s=960x540:d=65:r=5",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(fallback),
        ]
        result = subprocess.run(command, capture_output=True, text=True)  # noqa: S603
        if result.returncode != 0:
            raise RuntimeError(f"备用视频生成失败: {result.stderr.strip()}")
    return fallback, "系统生成的纯色工程备用视频（非教学素材）", True


def _load_steps(asset_id: str) -> list[dict[str, Any]]:
    source = (
        PROJECT_ROOT
        / "samples"
        / "public-videos"
        / "shoe-lacing"
        / "results"
        / "reviewed-sop-v3.json"
    )
    if not source.is_file():
        raise RuntimeError("缺少鞋带 SOP 结构文件，无法准备赛场课程")
    steps = json.loads(source.read_text(encoding="utf-8"))["steps"]
    for step in steps:
        step["teaching"]["source_asset_id"] = asset_id
        for evidence in step.get("evidence_refs", []):
            if evidence.get("kind") == "video":
                evidence["asset_id"] = asset_id
    return [SopStep.model_validate(step).model_dump(mode="json") for step in steps]


def _seed_completed_replay(db: Session, learner: User, sop: SopVersion) -> PracticeSession:
    existing = db.scalar(
        select(PracticeSession).where(
            PracticeSession.learner_id == learner.id,
            PracticeSession.sop_id == sop.id,
            PracticeSession.status == "finished",
        )
    )
    if existing is not None:
        return existing
    steps = [SopStep.model_validate(item) for item in sop.steps]
    state = initial_state(steps, 1_760_000_000_000)
    state.update(
        current_step_index=None,
        step_revision=len(steps) + 1,
        step_state="completed",
        completed=[step.index for step in steps],
        call_count=6,
        error_count=1,
        unknown_streak=0,
        step_errors={"2": 1},
        step_unknowns={"1": 1},
        feedback="全部步骤完成。",
        attempts=[
            {
                "step_index": step.index,
                "duration_ms": step.expected_duration_s * 1000 + (6000 if step.index == 2 else 0),
                "source": "engineering_replay",
                "completed_at_ms": 1_760_000_000_000 + step.index * 30_000,
            }
            for step in steps
        ],
    )
    session = PracticeSession(
        sop_id=sop.id,
        learner_id=learner.id,
        status="finished",
        revision=8,
        steps=sop.steps,
        state=state,
        allow_provider_export=True,
    )
    db.add(session)
    db.flush()
    for index, latency_ms in enumerate((3040, 3310, 3210, 3330, 3320, 4280), start=1):
        receipt = ProviderReceipt(
            provider="qwen",
            mode="recorded_validation",
            model_id="configured-multimodal-model",
            prompt_version="practice-verify-v2",
            input_digest=f"contest-replay-{index}",
            response_digest=f"contest-result-{index}",
            latency_ms=latency_ms,
        )
        db.add(receipt)
        db.flush()
        db.add(
            PracticeObservation(
                session_id=session.id,
                request_key=f"engineering-replay-{index}",
                step_revision=min(index, len(steps)),
                status="completed",
                frames=[],
                result={"engineering_replay": True},
                receipt_id=receipt.id,
            )
        )
    db.commit()
    return session


def seed_contest() -> int:
    init_db()
    with get_session_factory()() as db:
        teacher = _ensure_user(db, "teacher", UserRole.instructor, "演示教师")
        learner = _ensure_user(db, "student", UserRole.learner, "演示学员")
        _ensure_user(db, "observer", UserRole.observer, "评委观察员")
        existing = db.scalar(
            select(SopVersion)
            .where(
                SopVersion.process_key == "shoe_lacing_basic",
                SopVersion.status == SopPublishStatus.published,
            )
            .order_by(SopVersion.version.desc())
        )
        if existing is None:
            video, license_note, fallback = _source_video()
            (get_settings().data_root / "runtime").mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="contest_asset_", dir=get_settings().data_root / "runtime"
            ) as temporary:
                copied = Path(temporary) / video.name
                copied.write_bytes(video.read_bytes())
                asset = register_asset(
                    db,
                    kind="video",
                    filename=video.name,
                    temp_path=copied,
                    license_note=license_note,
                    authorized_by=teacher.id,
                    allow_provider_export=not fallback,
                )
            existing = SopVersion(
                process_key="shoe_lacing_basic",
                version=1,
                status=SopPublishStatus.published,
                steps=_load_steps(asset.id),
                course_meta={
                    "title": "四步学会平行鞋带穿法",
                    "description": "看标准示范，逐步完成穿带与收尾。",
                    "objectives": ["识别每一步完成状态", "独立完成平行鞋带穿法"],
                    "difficulty": "入门",
                    "audience": "零基础学习者",
                    "estimated_minutes": 6,
                },
                reference_asset_id=asset.id,
                source_asset_ids=[asset.id],
                compiled_by=teacher.id,
                published_at=utcnow(),
            )
            db.add(existing)
            db.commit()
            db.refresh(existing)
        replay = _seed_completed_replay(db, learner, existing)
        print(f"contest seed ready: course={existing.id} replay={replay.id}")
        print("accounts: teacher / student / observer; shared local password documented in DEMO.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(seed_contest())
