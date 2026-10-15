"""Qwen 授权评测的出站门禁、指标与断点测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.data.manifest import sha256_file
from gongxu.db.models import (
    AuditAction,
    AuditLog,
    SopPublishStatus,
    SopVersion,
    SourceAsset,
    UserRole,
)
from gongxu.evaluation import qwen
from gongxu.security.auth import create_user
from gongxu.vision.demo import grinder_disassembly_sop


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


def _enable_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GONGXU_LLM_MODE", "live")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen-test")
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    get_settings.cache_clear()


def _manifest_payload(video: Path, sop: SopVersion, *, checksum: str | None = None) -> dict:
    steps = [
        {"step_index": int(item["index"]), "judgement": "passed"} for item in sop.steps
    ]
    return {
        "schema_version": "qwen-authorized-evaluation-v1",
        "dataset_id": "team-owned-smoke",
        "evaluation_role": "development_validation",
        "provider_export_authorized": True,
        "authorization_scope": "team_owned_explicit_provider_processing",
        "authorization_note": "团队原创演示视频，已明确同意发送关键帧至 Qwen",
        "authorized_by_username": "evaluator",
        "process_key": sop.process_key,
        "sop_version_id": sop.id,
        "threshold_tuning_prohibited": True,
        "cases": [
            {
                "case_id": "case-pass-01",
                "video_path": video.name,
                "sha256": checksum or sha256_file(video),
                "authorization_note": "团队成员自愿录制并授权本次 API 评测",
                "expected_result": "pass",
                "expected_steps": steps,
            }
        ],
    }


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_preflight_accepts_only_complete_authorized_hashed_batch(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_qwen(monkeypatch)
    create_user(
        db,
        username="evaluator",
        password="correct-horse-42",
        role=UserRole.instructor,
    )
    sop = _published_sop(db)
    video = tmp_path / "owned.mp4"
    video.write_bytes(b"team-owned-video")
    manifest = _write_manifest(tmp_path, _manifest_payload(video, sop))

    result = qwen.preflight_manifest(db, manifest)

    assert result.manifest.dataset_id == "team-owned-smoke"
    assert result.cases[0].path == video.resolve()
    assert result.cases[0].sha256 == sha256_file(video)
    assert result.sop_step_indexes == [int(step["index"]) for step in sop.steps]


def test_preflight_hash_mismatch_stops_before_provider_or_asset_registration(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_qwen(monkeypatch)
    create_user(
        db,
        username="evaluator",
        password="correct-horse-42",
        role=UserRole.instructor,
    )
    sop = _published_sop(db)
    video = tmp_path / "owned.mp4"
    video.write_bytes(b"team-owned-video")
    payload = _manifest_payload(video, sop, checksum="0" * 64)
    manifest = _write_manifest(tmp_path, payload)
    provider_calls = 0

    def forbidden_provider_call(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("预检失败时不得构造或调用 Provider")

    monkeypatch.setattr("gongxu.providers.factory.get_provider", forbidden_provider_call)
    with pytest.raises(qwen.EvaluationManifestError, match="SHA256 不符"):
        qwen.preflight_manifest(db, manifest)

    assert provider_calls == 0


def test_manifest_rejects_missing_explicit_export_authorization(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_qwen(monkeypatch)
    create_user(
        db,
        username="evaluator",
        password="correct-horse-42",
        role=UserRole.instructor,
    )
    sop = _published_sop(db)
    video = tmp_path / "owned.mp4"
    video.write_bytes(b"team-owned-video")
    payload = _manifest_payload(video, sop)
    payload["provider_export_authorized"] = False
    manifest = _write_manifest(tmp_path, payload)

    with pytest.raises(qwen.EvaluationManifestError, match="清单校验失败"):
        qwen.preflight_manifest(db, manifest)


def test_calculate_metrics_keeps_execution_failures_separate() -> None:
    cases = [
        {
            "execution_status": "completed",
            "expected_result": "pass",
            "predicted_result": "pass",
            "expected_steps": {"1": "passed", "2": "failed"},
            "predicted_steps": {"1": "passed", "2": "failed"},
            "latency_seconds": 2.0,
            "provider_metrics": {"calls": 2, "failed_calls": 0, "total_tokens": 100},
        },
        {
            "execution_status": "completed",
            "expected_result": "fail",
            "predicted_result": "inconclusive",
            "expected_steps": {"1": "passed", "2": "failed"},
            "predicted_steps": {"1": "passed", "2": "insufficient_evidence"},
            "latency_seconds": 4.0,
            "provider_metrics": {"calls": 3, "failed_calls": 1, "total_tokens": 140},
        },
        {"execution_status": "failed", "expected_result": "fail"},
    ]

    result = qwen.calculate_metrics(cases)

    assert result["completed_count"] == 2
    assert result["execution_failure_count"] == 1
    assert result["result_accuracy"] == 0.5
    assert result["overall_three_state_accuracy"] == 0.5
    assert result["rejection_rate"] == 0.5
    assert result["provider_calls"] == 5
    assert result["provider_failed_calls"] == 1
    assert result["provider_total_tokens"] == 240
    assert result["latency_seconds_p50"] == 3.0
    assert result["latency_seconds_p95"] == 3.9
    assert result["step_macro_f1"] < 1.0


def test_rejection_only_smoke_does_not_claim_decision_accuracy() -> None:
    result = qwen.calculate_metrics(
        [
            {
                "execution_status": "completed",
                "expected_result": "inconclusive",
                "predicted_result": "inconclusive",
                "expected_steps": {"1": "insufficient_evidence"},
                "predicted_steps": {},
                "latency_seconds": 1.0,
                "provider_metrics": {},
            }
        ]
    )

    assert result["result_accuracy"] is None
    assert result["step_macro_f1"] is None
    assert result["overall_three_state_accuracy"] == 1.0
    assert result["step_all_label_macro_f1"] == 1.0


def test_run_evaluation_resumes_completed_case_without_rerun(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_qwen(monkeypatch)
    create_user(
        db,
        username="evaluator",
        password="correct-horse-42",
        role=UserRole.instructor,
    )
    sop = _published_sop(db)
    video = tmp_path / "owned.mp4"
    video.write_bytes(b"team-owned-video")
    manifest = _write_manifest(tmp_path, _manifest_payload(video, sop))
    preflight = qwen.preflight_manifest(db, manifest)
    output = tmp_path / "report.json"
    calls = 0

    def fake_execute(_db, _preflight, case):
        nonlocal calls
        calls += 1
        return {
            "case_id": case.case_id,
            "filename": case.filename,
            "sha256": case.sha256,
            "execution_status": "completed",
            "expected_result": "pass",
            "predicted_result": "pass",
            "expected_steps": {str(k): v for k, v in case.expected_steps.items()},
            "predicted_steps": {str(k): v for k, v in case.expected_steps.items()},
            "provider_metrics": {"calls": 1, "total_tokens": 10},
            "latency_seconds": 1.0,
        }

    monkeypatch.setattr(qwen, "_execute_case", fake_execute)
    first = qwen.run_evaluation(db, preflight, output_path=output)
    second = qwen.run_evaluation(db, preflight, output_path=output)

    assert calls == 1
    assert first["complete"] is True
    assert second["complete"] is True
    assert output.with_suffix(".md").exists()


def test_execute_case_reuses_succeeded_job_and_audits_provider_once(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_qwen(monkeypatch)
    create_user(
        db,
        username="evaluator",
        password="correct-horse-42",
        role=UserRole.instructor,
    )
    sop = _published_sop(db)
    video = tmp_path / "owned.mp4"
    video.write_bytes(b"team-owned-video")
    manifest = _write_manifest(tmp_path, _manifest_payload(video, sop))
    preflight = qwen.preflight_manifest(db, manifest)
    runner_calls = 0

    def fake_runner(_db, _job_id, work_dir):
        nonlocal runner_calls
        runner_calls += 1
        assert work_dir.is_dir()
        return {
            "status": "inconclusive",
            "report_id": None,
            "rejected": True,
            "reason_code": "quality_gate",
            "model_version": "qwen@qwen-test",
            "provider_metrics": {
                "provider": "qwen",
                "model_id": "qwen-test",
                "calls": 1,
                "successful_calls": 1,
                "failed_calls": 0,
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "latency_ms": 20,
                "pricing_status": "not_configured",
            },
        }

    monkeypatch.setattr(qwen.assessment_service, "run_recorded_assessment_job", fake_runner)
    first = qwen._execute_case(db, preflight, preflight.cases[0])
    second = qwen._execute_case(db, preflight, preflight.cases[0])

    assert first["predicted_result"] == "inconclusive"
    assert second["recovered_from_job"] is True
    assert runner_calls == 1
    assert db.query(SourceAsset).count() == 1
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == AuditAction.provider_call)
        .count()
        == 1
    )
