"""Recorded-assessment cache, ownership, and provider-consent regression tests."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gongxu.assessment import service as assessment_service
from gongxu.config import get_settings
from gongxu.db.models import (
    AssessmentSession,
    Job,
    Observation,
    ScoreReport,
    SopPublishStatus,
    SopVersion,
    SourceAsset,
    StepAssessment,
    UserRole,
)
from gongxu.jobs import service as jobs_service
from gongxu.main import create_app
from gongxu.security.auth import create_user
from gongxu.sop.fixtures import PROCESS_KEY, grinder_disassembly_sop
from gongxu.sop.schema import SopStep
from gongxu.vision import pipeline


def _login(db: Session, username: str, role: UserRole) -> tuple[TestClient, str]:
    user = create_user(
        db,
        username=username,
        password="correct-horse-42",
        role=role,
    )
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct-horse-42"},
    )
    assert response.status_code == 200
    return client, user.id


def _published_sop(db: Session) -> SopVersion:
    fixture = grinder_disassembly_sop()
    row = SopVersion(
        process_key=fixture.process_key,
        version=fixture.version,
        status=SopPublishStatus.published,
        source_grade=fixture.source_grade.value,
        steps=[step.model_dump(mode="json") for step in fixture.steps],
        scoring_profile=fixture.scoring_profile.model_dump(mode="json"),
        prompt_version=fixture.prompt_version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _all_passed_judgements() -> list[dict[str, object]]:
    steps = grinder_disassembly_sop().steps
    return [
        {
            "step_index": step.index,
            "judgement": "passed",
            "start_ms": step.index * 1_000,
            "end_ms": step.index * 1_000 + 800,
            "rule_code": "observed",
            "reason": "检测到对应动作",
            "confidence": 0.91,
            "keyframe_ref": f"frame-{step.index}",
            "observable_check_results": {
                check: "passed" for check in step.observable_checks
            },
        }
        for step in steps
    ]


def _submit(
    client: TestClient,
    sop_id: str,
    content: bytes = b"same-video",
    *,
    allow_provider_export: bool = False,
):
    return client.post(
        "/api/v1/assessment-sessions/submit-video",
        files={"file": ("learner.mp4", io.BytesIO(content), "video/mp4")},
        data={
            "sop_version_id": sop_id,
            "allow_provider_export": str(allow_provider_export).lower(),
        },
    )


def _run_vision_job(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    session_id: str,
    job_id: str,
    model_version: str = "test-vision-v1",
    evidence_source: str = "vision",
    complete_job: bool = True,
) -> tuple[dict[str, object], ScoreReport]:
    """Run the real worker service around a deterministic fake vision pipeline."""

    monkeypatch.setattr(pipeline, "_model_version", lambda *args, **kwargs: model_version)

    def _fake_pipeline(db_session, session, **kwargs):
        first_step = grinder_disassembly_sop().steps[0]
        db_session.add(
            Observation(
                session_id=session.id,
                timestamp_ms=1_250,
                hand="right",
                tool="power_plug",
                object="power_outlet",
                action="unplug",
                relation="right_holds_power_plug",
                confidence=0.93,
                frame_hash="frame-hash",
                payload={
                    "phase": "during",
                    "observable_check_results": {
                        check: "passed" for check in first_step.observable_checks
                    },
                },
            )
        )
        db_session.commit()
        return {
            "judgements": _all_passed_judgements(),
            "observations": 1,
            "quality": {"ok": True, "visible_ratio": 1.0},
            "evidence_source": evidence_source,
            "model_version": model_version,
        }

    monkeypatch.setattr(pipeline, "run_recorded_pipeline", _fake_pipeline)
    result = assessment_service.run_recorded_assessment_job(
        db,
        job_id,
        tmp_path / f"job-{job_id}",
    )
    report = db.get(ScoreReport, str(result["report_id"]))
    assert report is not None
    if not complete_job:
        return result, report
    completed_job = jobs_service.complete(db, job_id, result=result)
    assert completed_job.result is not None
    return completed_job.result, report


def test_completed_result_cache_copies_evidence_without_enqueuing_worker(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sop = _published_sop(db)
    learner, learner_id = _login(db, "cache-learner", UserRole.learner)
    _login(db, "cache-instructor", UserRole.instructor)
    monkeypatch.setattr(pipeline, "_model_version", lambda *args, **kwargs: "test-vision-v1")

    first = _submit(learner, sop.id)
    assert first.status_code == 202, first.text
    first_body = first.json()
    assert first_body["cache_hit"] is False
    source_session_id = first_body["session"]["id"]
    source_job_id = first_body["job_id"]

    _, source_report = _run_vision_job(
        db,
        monkeypatch,
        tmp_path,
        session_id=source_session_id,
        job_id=source_job_id,
    )
    source_report_id = source_report.id

    repeated = _submit(learner, sop.id)
    assert repeated.status_code == 202, repeated.text
    body = repeated.json()
    assert body["cache_hit"] is True
    assert body["cache_source_session_id"] == source_session_id
    assert body["job_status"] == "succeeded"
    assert body["session"]["status"] == "completed"
    assert body["session"]["learner_id"] == learner_id
    assert body["session"]["id"] != source_session_id

    target_session_id = body["session"]["id"]
    target_report = db.query(ScoreReport).filter_by(session_id=target_session_id).one()
    source_report = db.get(ScoreReport, source_report_id)
    assert source_report is not None
    assert target_report.id != source_report.id
    assert target_report.total_score == source_report.total_score
    assert target_report.dimension_scores == source_report.dimension_scores
    assert target_report.model_version == source_report.model_version
    assert db.query(Observation).filter_by(session_id=target_session_id).count() == 1
    copied_observation = db.query(Observation).filter_by(session_id=target_session_id).one()
    assert copied_observation.payload == {
        "phase": "during",
        "observable_check_results": {
            check: "passed"
            for check in grinder_disassembly_sop().steps[0].observable_checks
        },
    }
    assert db.query(StepAssessment).filter_by(session_id=target_session_id).count() == 7
    source_steps = (
        db.query(StepAssessment)
        .filter_by(session_id=source_session_id)
        .order_by(StepAssessment.step_index)
        .all()
    )
    target_steps = (
        db.query(StepAssessment)
        .filter_by(session_id=target_session_id)
        .order_by(StepAssessment.step_index)
        .all()
    )
    assert [step.observable_check_results for step in target_steps] == [
        step.observable_check_results for step in source_steps
    ]
    assert all(step.observable_check_results for step in target_steps)

    cached_job = db.get(Job, body["job_id"])
    assert cached_job is not None
    assert cached_job.result is not None
    assert cached_job.result["cache_hit"] is True
    assert cached_job.result["source_session_id"] == source_session_id
    assert cached_job.result["coaching"]["source"] == "deterministic_rules"

    source_report_response = learner.get(
        f"/api/v1/assessment-sessions/{source_session_id}/report"
    )
    assert source_report_response.status_code == 200
    source_coaching = source_report_response.json()["coaching"]
    assert source_coaching["source"] == "deterministic_rules"
    assert "score" not in source_coaching

    cached_report_response = learner.get(
        f"/api/v1/assessment-sessions/{target_session_id}/report"
    )
    assert cached_report_response.status_code == 200
    assert cached_report_response.json()["coaching"] == source_coaching
    assert cached_report_response.json()["analysis_duration_ms"] >= 0

    progress = learner.get(f"/api/v1/assessment-sessions/{target_session_id}/progress")
    assert progress.status_code == 200
    assert progress.json()["report_ready"] is True
    assert progress.json()["job"]["progress_percent"] == 100


def test_assessment_cache_is_scoped_to_uploading_user(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sop = _published_sop(db)
    first_learner, _ = _login(db, "owner-one", UserRole.learner)
    _login(db, "owner-instructor", UserRole.instructor)
    monkeypatch.setattr(pipeline, "_model_version", lambda *args, **kwargs: "test-vision-v1")
    first = _submit(first_learner, sop.id)
    source_session_id = first.json()["session"]["id"]
    source_job_id = first.json()["job_id"]
    _run_vision_job(
        db,
        monkeypatch,
        tmp_path,
        session_id=source_session_id,
        job_id=source_job_id,
    )

    second_learner, second_id = _login(db, "owner-two", UserRole.learner)
    submitted = _submit(second_learner, sop.id)
    assert submitted.status_code == 202
    assert submitted.json()["cache_hit"] is False
    assert submitted.json()["job_status"] == "queued"
    assert submitted.json()["session"]["learner_id"] == second_id


def test_recorded_job_gates_external_coaching_on_asset_consent(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from gongxu.assessment import coaching as coaching_module

    sop = _published_sop(db)
    learner, _ = _login(db, "coaching-consent-owner", UserRole.learner)
    submitted = _submit(
        learner,
        sop.id,
        b"coaching-consent-video",
        allow_provider_export=True,
    )
    assert submitted.status_code == 202
    body = submitted.json()
    calls: list[dict[str, object]] = []

    def _fake_coaching(db_session, **kwargs):
        del db_session
        calls.append(kwargs)
        return {
            "version": "coaching-v1",
            "source": "external_model",
            "explanation": "按证据复练第二步。",
            "practice_plan": {
                "focus_steps": ["step_02"],
                "repeat_count": 3,
                "note": "完成后重新提交。",
            },
            "provider": "test-provider",
            "model_id": "test-model",
            "receipt_ids": [],
            "errors": [],
        }

    monkeypatch.setattr(coaching_module, "generate_coaching", _fake_coaching)
    result, report = _run_vision_job(
        db,
        monkeypatch,
        tmp_path,
        session_id=body["session"]["id"],
        job_id=body["job_id"],
    )

    assert result["coaching"]["source"] == "external_model"
    assert len(calls) == 1
    assert calls[0]["external_allowed"] is True
    assert "total_score" not in calls[0]["report"]
    assert all("keyframe_ref" not in step for step in calls[0]["step_results"])
    assert all("observable_check_results" in step for step in calls[0]["step_results"])
    assert report.total_score == result["total_score"]


def test_scoring_v2_result_is_not_reused_by_scoring_v3(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sop = _published_sop(db)
    learner, _ = _login(db, "old-score-owner", UserRole.learner)
    monkeypatch.setattr(pipeline, "_model_version", lambda *args, **kwargs: "test-vision-v1")
    first = _submit(learner, sop.id, b"old-scoring-result")
    source_session_id = first.json()["session"]["id"]
    source_job_id = first.json()["job_id"]
    result, report = _run_vision_job(
        db,
        monkeypatch,
        tmp_path,
        session_id=source_session_id,
        job_id=source_job_id,
    )
    assert report.scoring_rules_version == assessment_service.SCORING_RULES_VERSION

    report.scoring_rules_version = "scoring-v2"
    source_job = db.get(Job, source_job_id)
    assert source_job is not None
    source_job.result = {**(source_job.result or result), "scoring_rules_version": "scoring-v2"}
    source_job.checkpoint = {
        **(source_job.checkpoint or {}),
        "report_scoring_rules_version": "scoring-v2",
    }
    db.commit()

    repeated = _submit(learner, sop.id, b"old-scoring-result")

    assert repeated.status_code == 202
    assert repeated.json()["cache_hit"] is False
    assert repeated.json()["job_status"] == "queued"


def test_evaluate_rejects_observable_checks_not_declared_by_sop(db: Session) -> None:
    sop = _published_sop(db)
    session = assessment_service.create_session(
        db,
        process_key=sop.process_key,
        mode="manual_offline",
        sop_version_id=sop.id,
    )

    with pytest.raises(assessment_service.AssessmentError, match="未声明的视觉检查"):
        assessment_service.evaluate(
            db,
            session.id,
            judgements=[
                {
                    "step_index": 1,
                    "judgement": "passed",
                    "observable_check_results": {"模型自行添加的检查": "passed"},
                }
            ],
        )

    assert db.query(StepAssessment).filter_by(session_id=session.id).count() == 0
    assert db.query(ScoreReport).filter_by(session_id=session.id).count() == 0


def test_completion_check_is_accepted_as_persisted_visual_evidence() -> None:
    step = SopStep(
        id="step-1",
        index=1,
        name="安装滤芯",
        objects=["filter"],
        actions=["install"],
        completion_checks=["滤芯已就位"],
        weight=100,
    )

    cleaned = assessment_service._clean_observable_check_results(
        step,
        {"滤芯已就位": "passed"},
    )

    assert cleaned == {"滤芯已就位": "passed"}


def test_fixture_evidence_is_never_used_as_assessment_cache(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sop = _published_sop(db)
    learner, _ = _login(db, "fixture-cache-owner", UserRole.learner)
    _login(db, "fixture-cache-instructor", UserRole.instructor)
    monkeypatch.setattr(pipeline, "_model_version", lambda *args, **kwargs: "demo-fixture")
    first = _submit(learner, sop.id)
    source_session_id = first.json()["session"]["id"]
    source_job_id = first.json()["job_id"]
    _run_vision_job(
        db,
        monkeypatch,
        tmp_path,
        session_id=source_session_id,
        job_id=source_job_id,
        model_version="demo-fixture",
        evidence_source="fixture",
    )

    repeated = _submit(learner, sop.id)
    assert repeated.status_code == 202
    assert repeated.json()["cache_hit"] is False
    assert repeated.json()["job_status"] == "queued"


def test_learner_cannot_read_report_for_unowned_instructor_session(db: Session) -> None:
    sop = _published_sop(db)
    instructor, _ = _login(db, "report-instructor", UserRole.instructor)
    learner, _ = _login(db, "report-learner", UserRole.learner)
    created = instructor.post(
        "/api/v1/assessment-sessions",
        json={
            "process_key": PROCESS_KEY,
            "sop_version_id": sop.id,
            "mode": "manual_offline",
        },
    )
    report = instructor.post(
        f"/api/v1/assessment-sessions/{created.json()['id']}/evaluate",
        json={"judgements": _all_passed_judgements(), "model_version": "manual-test"},
    )
    assert report.status_code == 200
    assert report.json()["model_version"] == assessment_service.MANUAL_REVIEW_MODEL_VERSION

    denied = learner.get(f"/api/v1/reports/{report.json()['id']}")
    assert denied.status_code == 403


def test_manual_report_cannot_poison_worker_recovery_or_cache(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sop = _published_sop(db)
    learner, learner_id = _login(db, "poison-owner", UserRole.learner)
    instructor, _ = _login(db, "poison-instructor", UserRole.instructor)
    monkeypatch.setattr(pipeline, "_model_version", lambda *args, **kwargs: "test-vision-v1")

    submitted = _submit(learner, sop.id, b"poison-attempt-video")
    assert submitted.status_code == 202
    session_id = submitted.json()["session"]["id"]
    job_id = submitted.json()["job_id"]

    # The public manual endpoint cannot complete an active automatic session.
    denied = instructor.post(
        f"/api/v1/assessment-sessions/{session_id}/evaluate",
        json={"judgements": _all_passed_judgements(), "model_version": "test-vision-v1"},
    )
    assert denied.status_code == 400
    assert db.query(ScoreReport).filter_by(session_id=session_id).count() == 0

    # Simulate a pre-fix/DB-level injected report. It has no transactional job
    # provenance, so it is neither cacheable nor recoverable by the worker.
    poisoned_session = db.get(AssessmentSession, session_id)
    assert poisoned_session is not None
    poisoned = ScoreReport(
        session_id=session_id,
        total_score=100,
        result="pass",
        dimension_scores={"accuracy": 100},
        evidence_coverage=1,
        model_version="test-vision-v1",
        input_hash=poisoned_session.input_sha256 or "",
        scoring_rules_version=assessment_service.SCORING_RULES_VERSION,
    )
    db.add(poisoned)
    poisoned_session.status = assessment_service.AssessmentStatus.completed
    db.commit()
    db.refresh(poisoned)

    assert (
        assessment_service.find_completed_assessment_cache(
            db,
            owner_user_id=learner_id,
            input_sha256=poisoned_session.input_sha256 or "",
            sop_version_id=sop.id,
            model_version="test-vision-v1",
        )
        is None
    )

    worker_result, genuine_report = _run_vision_job(
        db,
        monkeypatch,
        tmp_path,
        session_id=session_id,
        job_id=job_id,
    )
    assert worker_result.get("recovered") is not True
    assert genuine_report.id != poisoned.id
    assert db.get(ScoreReport, poisoned.id) is None
    assert genuine_report.model_version == "test-vision-v1"


def test_worker_recovers_only_transactionally_bound_automatic_report(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sop = _published_sop(db)
    learner, _ = _login(db, "recovery-owner", UserRole.learner)
    monkeypatch.setattr(pipeline, "_model_version", lambda *args, **kwargs: "test-vision-v1")
    submitted = _submit(learner, sop.id, b"recovery-video")
    session_id = submitted.json()["session"]["id"]
    job_id = submitted.json()["job_id"]

    first_result, report = _run_vision_job(
        db,
        monkeypatch,
        tmp_path,
        session_id=session_id,
        job_id=job_id,
        complete_job=False,
    )
    assert first_result.get("recovered") is not True
    assert db.get(Job, job_id).result is None

    recovered = assessment_service.run_recorded_assessment_job(
        db,
        job_id,
        tmp_path / "recovered-job",
    )
    assert recovered["recovered"] is True
    assert recovered["report_id"] == report.id
    assert recovered["evidence_source"] == "vision"
    assert recovered["scoring_rules_version"] == assessment_service.SCORING_RULES_VERSION
    assert recovered["coaching"] == first_result["coaching"]
    assert recovered["coaching"]["source"] == "deterministic_rules"

    # A committed report is still an internal checkpoint until the worker marks
    # the exact job succeeded; the UI must keep polling for coaching/reject details.
    still_running = learner.get(f"/api/v1/assessment-sessions/{session_id}/progress")
    assert still_running.status_code == 200
    assert still_running.json()["report_ready"] is False
    jobs_service.complete(db, job_id, result=recovered)
    completed = learner.get(f"/api/v1/assessment-sessions/{session_id}/progress")
    assert completed.status_code == 200
    assert completed.json()["report_ready"] is True


def test_qwen_video_requires_provider_export_consent_before_adapter(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    get_settings.cache_clear()
    sop = _published_sop(db)
    asset = SourceAsset(
        kind="video",
        filename="private.mp4",
        sha256="a" * 64,
        authorized_by="learner-id",
        allow_provider_export=False,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    session = AssessmentSession(
        mode="recorded",
        process_key=PROCESS_KEY,
        sop_version_id=sop.id,
        learner_id="learner-id",
        input_asset_id=asset.id,
        input_sha256=asset.sha256,
    )
    db.add(session)
    db.commit()

    def _must_not_resolve(*args, **kwargs):
        raise AssertionError("未授权视频不应进入外部视觉适配器")

    monkeypatch.setattr(pipeline, "resolve_adapter", _must_not_resolve)
    with pytest.raises(pipeline.RejectError) as exc_info:
        pipeline.run_recorded_pipeline(db, session, work_dir=tmp_path)
    assert exc_info.value.reason_code == "provider_export_not_authorized"

    job = jobs_service.enqueue(
        db,
        job_type="recorded_assessment",
        payload={"session_id": session.id, "requested_by": "learner-id"},
        idempotency_key=f"assess:{session.id}",
    )
    result = assessment_service.run_recorded_assessment_job(db, job.id, tmp_path / "job")
    assert result["rejected"] is True
    assert result["reason_code"] == "provider_export_not_authorized"
    assert result["model_version"].startswith("qwen@")
    assert result["model_version"].endswith(":not-invoked")

    recovered = assessment_service.run_recorded_assessment_job(
        db,
        job.id,
        tmp_path / "job-recovered",
    )
    assert recovered["recovered"] is True
    assert recovered["report_id"] == result["report_id"]
    assert recovered["evidence_source"] == "vision_rejection"
