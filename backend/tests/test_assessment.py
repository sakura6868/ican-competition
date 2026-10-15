"""考评 API 集成测试：创建会话 → 提交判定 → 确定性报告。"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from gongxu.assessment import service as assessment_service
from gongxu.db.models import AssessmentSession, Job, Observation, UserRole
from gongxu.jobs import service as jobs_service
from gongxu.main import create_app
from gongxu.security.auth import create_user
from gongxu.sop.fixtures import grinder_disassembly_sop


def _make_instructor(db, username: str = "inst1"):
    return create_user(db, username=username, password="correct-horse-42", role=UserRole.instructor)


def _make_learner(db, username: str = "stu1"):
    return create_user(db, username=username, password="correct-horse-42", role=UserRole.learner)


def _login(db, username: str) -> TestClient:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "correct-horse-42"}
    )
    assert resp.status_code == 200
    return client


def _inject_explicit_replay(monkeypatch) -> None:
    """Use the deterministic replay provider only as an explicit test double."""
    from gongxu.providers.replay import ReplayProvider

    monkeypatch.setattr("gongxu.api.routes.sops.get_provider", lambda: ReplayProvider())


def _all_passed_judgements() -> list[dict]:
    steps = grinder_disassembly_sop().steps
    return [
        {
            "step_index": step.index,
            "judgement": "passed",
            "start_ms": 1000,
            "end_ms": 3000,
            # Manual reviewer explicitly verified this step's visible checks;
            # a plain "passed" judgement must not imply safety evidence.
            "rule_code": "observed_checks_passed",
            "keyframe_ref": f"frame-{step.index}",
            "observable_check_results": {
                check: "passed" for check in step.observable_checks
            },
        }
        for step in steps
    ]


def test_full_assessment_flow(db, video_asset, monkeypatch) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_instructor(db)
    inst = _login(db, "inst1")

    # 编译并发布 SOP
    compiled = inst.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert compiled.status_code == 201
    sop_id = compiled.json()["id"]
    assert inst.post(f"/api/v1/sops/{sop_id}/publish").status_code == 200

    # 创建考评会话（学员）
    _make_learner(db)
    learner = _login(db, "stu1")
    created = learner.post(
        "/api/v1/assessment-sessions",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "mode": "recorded",
            "sop_version_id": sop_id,
        },
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert created.json()["status"] == "created"

    # 提交判定 → 评分报告（instructor 执行）
    eval_resp = inst.post(
        f"/api/v1/assessment-sessions/{session_id}/evaluate",
        json={"judgements": _all_passed_judgements(), "model_version": "fixture-v1"},
    )
    assert eval_resp.status_code == 200
    report = eval_resp.json()
    assert report["result"] == "pass"
    assert report["total_score"] >= 95.0
    assert report["evidence_coverage"] == 1.0
    assert report["scoring_rules_version"] == assessment_service.SCORING_RULES_VERSION

    session_report = inst.get(f"/api/v1/assessment-sessions/{session_id}/report")
    assert session_report.status_code == 200
    assert session_report.json()["steps"][0]["observable_check_results"] == {
        check: "passed" for check in grinder_disassembly_sop().steps[0].observable_checks
    }

    # 查询报告
    fetched = inst.get(f"/api/v1/reports/{report['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["total_score"] == report["total_score"]

    # 重复评分被拒绝（确定性）
    again = inst.post(
        f"/api/v1/assessment-sessions/{session_id}/evaluate",
        json={"judgements": _all_passed_judgements(), "model_version": "fixture-v1"},
    )
    assert again.status_code == 400


def test_critical_missing_marks_fail(db, video_asset, monkeypatch) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_instructor(db)
    inst = _login(db, "inst1")
    compiled = inst.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    ).json()
    inst.post(f"/api/v1/sops/{compiled['id']}/publish")

    session = inst.post(
        "/api/v1/assessment-sessions",
        json={"process_key": "impact_grinder_safe_subassembly", "mode": "manual_offline"},
    ).json()

    judgements = [
        {
            "step_index": 1,
            "judgement": "failed",  # 关键步骤 1 明确判失败（错工具/漏做）
            "start_ms": 0,
            "end_ms": 1000,
            "rule_code": "wrong_tool",
            "reason": "关键步未正确执行",
        }
    ] + [
        {
            "step_index": i,
            "judgement": "passed",
            "start_ms": 1000,
            "end_ms": 3000,
            "rule_code": "observed_checks_passed",
        }
        for i in range(2, 8)  # 步骤 2-7 通过
    ]
    report = inst.post(
        f"/api/v1/assessment-sessions/{session['id']}/evaluate",
        json={"judgements": judgements, "model_version": "fixture-v1"},
    ).json()
    assert report["result"] == "fail"
    assert report["total_score"] <= 59.0


def test_learner_cannot_evaluate(db) -> None:
    _make_learner(db)
    learner = _login(db, "stu1")
    resp = learner.post(
        "/api/v1/assessment-sessions",
        json={"process_key": "p", "mode": "manual_offline"},
    )
    # learner 可以创建自己的会话
    assert resp.status_code == 201
    # 但不能评分
    eval_resp = learner.post(
        f"/api/v1/assessment-sessions/{resp.json()['id']}/evaluate",
        json={"judgements": [{"step_index": 1, "judgement": "passed"}]},
    )
    assert eval_resp.status_code == 403


def test_evaluate_requires_published_sop(db) -> None:
    _make_instructor(db)
    inst = _login(db, "inst1")
    session = inst.post(
        "/api/v1/assessment-sessions",
        json={"process_key": "impact_grinder_safe_subassembly", "mode": "manual_offline"},
    ).json()
    resp = inst.post(
        f"/api/v1/assessment-sessions/{session['id']}/evaluate",
        json={"judgements": [{"step_index": 1, "judgement": "passed"}]},
    )
    # 没有已发布 SOP
    assert resp.status_code == 400


def test_formal_recorded_session_requires_explicit_published_sop(db) -> None:
    _make_instructor(db)
    instructor = _login(db, "inst1")

    response = instructor.post(
        "/api/v1/assessment-sessions",
        json={"process_key": "impact_grinder_safe_subassembly", "mode": "recorded"},
    )

    assert response.status_code == 400
    assert "必须绑定明确的已发布 SOP" in response.json()["detail"]


def test_removed_live_session_mode_is_rejected(db) -> None:
    _make_instructor(db)
    instructor = _login(db, "inst1")

    response = instructor.post(
        "/api/v1/assessment-sessions",
        json={"process_key": "impact_grinder_safe_subassembly", "mode": "live"},
    )

    assert response.status_code == 422


def test_historical_unbound_recorded_session_fails_closed_before_processing(
    db, video_asset, tmp_path
) -> None:
    legacy = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        sop_version_id=None,
        input_asset_id=video_asset,
    )
    db.add(legacy)
    db.commit()
    db.refresh(legacy)
    sentinel = Observation(
        session_id=legacy.id,
        timestamp_ms=100,
        action="legacy-partial-observation",
    )
    db.add(sentinel)
    db.commit()
    job = jobs_service.enqueue(
        db,
        job_type="recorded_assessment",
        payload={"session_id": legacy.id},
        idempotency_key=f"assess:{legacy.id}",
    )

    with pytest.raises(assessment_service.AssessmentError, match="未绑定明确的 SOP"):
        assessment_service.run_recorded_assessment_job(db, job.id, tmp_path)

    assert db.query(Observation).filter(Observation.session_id == legacy.id).count() == 1


def test_historical_unbound_recorded_session_cannot_be_enqueued(db, video_asset) -> None:
    _make_instructor(db)
    instructor = _login(db, "inst1")
    legacy = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        sop_version_id=None,
        input_asset_id=video_asset,
    )
    db.add(legacy)
    db.commit()
    db.refresh(legacy)

    response = instructor.post(f"/api/v1/assessment-sessions/{legacy.id}/process")

    assert response.status_code == 400
    assert "历史考评会话未绑定明确的 SOP" in response.json()["detail"]
    assert db.query(Job).count() == 0


def test_submit_video_progress_and_evidence_workflow(db, video_asset, monkeypatch) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_instructor(db)
    instructor = _login(db, "inst1")
    compiled = instructor.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    ).json()
    instructor.post(f"/api/v1/sops/{compiled['id']}/publish")

    _make_learner(db)
    learner = _login(db, "stu1")
    submitted = learner.post(
        "/api/v1/assessment-sessions/submit-video",
        files={"file": ("learner.mp4", io.BytesIO(b"recorded-video"), "video/mp4")},
        data={"sop_version_id": compiled["id"]},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    session_id = body["session"]["id"]
    assert body["session"]["input_asset_id"]
    assert body["session"]["sop_version_id"] == compiled["id"]
    assert body["session"]["job_id"] == body["job_id"]

    progress = learner.get(f"/api/v1/assessment-sessions/{session_id}/progress")
    assert progress.status_code == 200
    assert progress.json()["session_status"] == "processing"
    assert progress.json()["job"]["status"] == "queued"

    assessment_job = db.get(Job, body["job_id"])
    assert assessment_job is not None
    evaluated = assessment_service.evaluate(
        db,
        session_id,
        judgements=_all_passed_judgements(),
        model_version="test-vision-v1",
        recorded_job=assessment_job,
        evidence_source="vision",
    )
    jobs_service.complete(
        db,
        assessment_job.id,
        result={
            "session_id": session_id,
            "report_id": evaluated.id,
            "total_score": evaluated.total_score,
            "result": evaluated.result,
            "evidence_coverage": evaluated.evidence_coverage,
            "evidence_source": "vision",
            "model_version": evaluated.model_version,
            "scoring_rules_version": evaluated.scoring_rules_version,
        },
    )

    # 证据接口只把步骤时间区间内已落库的事实聚合到该步骤，且保持时间顺序去重。
    db.add_all(
        [
            Observation(
                session_id=session_id,
                timestamp_ms=1500,
                hand="right",
                tool="wrench",
                object="locking_nut",
                action="loosen",
                relation="right_holds_wrench",
                confidence=0.91,
                frame_hash="frame-1",
            ),
            Observation(
                session_id=session_id,
                timestamp_ms=1800,
                hand="right",
                tool="wrench",
                object="guard",
                action="remove",
                relation="right_holds_wrench",
                confidence=0.88,
            ),
            Observation(
                session_id=session_id,
                timestamp_ms=5000,
                hand="right",
                tool="brush",
                object="workbench",
                action="clean",
                relation="right_holds_brush",
                confidence=0.95,
            ),
        ]
    )
    db.commit()

    evidence = learner.get(f"/api/v1/assessment-sessions/{session_id}/evidence")
    assert evidence.status_code == 200
    evidence_body = evidence.json()
    assert len(evidence_body["steps"]) == 7
    first_step = evidence_body["steps"][0]
    assert first_step["detected_tools"] == ["wrench"]
    assert first_step["detected_objects"] == ["locking_nut", "guard"]
    assert first_step["detected_actions"] == ["loosen", "remove"]
    assert "brush" not in first_step["detected_tools"]
    assert first_step["evidence_url"]
    assert first_step["evidence_timestamp_ms"] == 1500
    assert evidence_body["steps"][1]["evidence_url"] is None
    assert evidence_body["steps"][1]["evidence_timestamp_ms"] is None
    assert evidence.json()["preview_url"] == body["preview_url"]
    assert learner.get(body["preview_url"]).content == b"recorded-video"

    extracted_at: list[int] = []

    def _fake_extract_frame(src, dst, *, timestamp_ms, width=640):
        extracted_at.append(timestamp_ms)
        dst.write_bytes(b"evidence-jpeg")
        return dst

    monkeypatch.setattr("gongxu.media.video.extract_frame_at", _fake_extract_frame)
    frame = learner.get(first_step["evidence_url"])
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/jpeg")
    assert frame.content == b"evidence-jpeg"
    assert extracted_at == [1500]

    missing_frame = learner.get(
        f"/api/v1/assessment-sessions/{session_id}/evidence-frames/2"
    )
    assert missing_frame.status_code == 404
    assert "观测记录不匹配" in missing_frame.json()["detail"]

    # 报告和证据端点共享同一 StepJudgementOut 契约。
    report = learner.get(f"/api/v1/assessment-sessions/{session_id}/report")
    assert report.status_code == 200
    assert report.json()["steps"][0]["detected_actions"] == ["loosen", "remove"]
    assert report.json()["steps"][0]["observable_check_results"] == {
        check: "passed" for check in grinder_disassembly_sop().steps[0].observable_checks
    }
