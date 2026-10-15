"""视觉事实层测试：观察构建、质量门控、确定性对齐、fixture、预录闭环。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from gongxu.assessment.service import run_recorded_assessment_job
from gongxu.config import get_settings
from gongxu.db.models import Observation, StepAssessment
from gongxu.scoring.engine import score_assessment
from gongxu.sop.fixtures import grinder_disassembly_sop
from gongxu.sop.schema import SopStep
from gongxu.vision import align, facts
from gongxu.vision.fixture import fixture_detections


def _detections(confidences: list[float], *, ts_step: int = 500) -> list[facts.Detection]:
    return [
        facts.Detection(
            timestamp_ms=i * ts_step,
            hand="right",
            tool="wrench",
            object="locking_nut",
            action="loosen",
            confidence=c,
            frame_hash=f"f{i}",
        )
        for i, c in enumerate(confidences)
    ]


def test_quality_gate_rejects_low_confidence() -> None:
    dets = _detections([0.2, 0.3, 0.4], ts_step=1000)
    ok, reason = facts.quality_gate(dets, total_frames=10)
    assert ok is False
    assert "60%" in reason  # 可见比例不足


def test_reference_evidence_does_not_dilute_visible_frame_ratio() -> None:
    general = _detections([0.95, 0.95, 0.95], ts_step=1000)
    reference = facts.Detection(
        timestamp_ms=3500,
        tool="wrench",
        object="locking_nut",
        action="loosen",
        confidence=0.98,
        frame_hash="reference-only",
        candidate_step_index=3,
        quality_eligible=False,
    )

    assert facts.visible_frame_ratio([*general, reference], total_frames=3) == 1.0


def test_superseded_detection_is_excluded_from_alignment_only() -> None:
    superseded = facts.Detection(
        timestamp_ms=1000,
        tool="wrench",
        object="locking_nut",
        action="loosen",
        confidence=0.98,
        frame_hash="coarse",
        candidate_step_index=3,
        alignment_eligible=False,
    )

    assert facts.visible_frame_ratio([superseded], total_frames=1) == 1.0
    assert facts.build_observations([superseded]) == []


def test_build_observations_requires_continuous_span() -> None:
    # 无候选步骤的单帧高置信仍不构成 ≥1s 连续证据
    dets = _detections([0.95], ts_step=1000)
    assert facts.build_observations(dets) == []

    # 多帧跨 ≥1s → 形成观察
    dets = _detections([0.95, 0.9, 0.93], ts_step=1000)
    obs = facts.build_observations(dets)
    assert len(obs) == 1
    assert obs[0].tool == "wrench"
    assert obs[0].relation_key == "right||wrench||locking_nut||loosen"


def test_build_observations_allows_downweighted_short_candidate_evidence() -> None:
    det = _detections([0.95])[0]
    det.candidate_step_index = 3
    det.evidence_reason = "扳手正在转动锁紧螺母"

    observations = facts.build_observations([det])

    assert len(observations) == 1
    assert observations[0].candidate_step_index == 3
    assert observations[0].sample_count == 1
    assert observations[0].confidence == 0.7125
    assert "已降权" in observations[0].evidence_reason


def test_build_observations_merges_complementary_candidate_frames() -> None:
    detections = [
        facts.Detection(
            timestamp_ms=1000,
            hand="right",
            tool="wrench",
            confidence=0.92,
            frame_hash="tool-frame",
            candidate_step_index=4,
        ),
        facts.Detection(
            timestamp_ms=1400,
            hand="right",
            object="locking_nut",
            action="loosen",
            confidence=0.9,
            frame_hash="action-frame",
            candidate_step_index=4,
        ),
    ]

    observations = facts.build_observations(detections)

    assert len(observations) == 1
    assert observations[0].relation_key == "right||wrench||locking_nut||loosen"
    assert observations[0].sample_count == 2
    assert observations[0].confidence == 0.6825
    assert (observations[0].start_ms, observations[0].end_ms) == (1000, 1400)


def test_build_observations_does_not_merge_conflicting_or_incomplete_candidate_fields() -> None:
    detections = [
        facts.Detection(
            timestamp_ms=1000,
            tool="wrench",
            object="locking_nut",
            confidence=0.93,
            frame_hash="wrench-frame",
            candidate_step_index=4,
        ),
        facts.Detection(
            timestamp_ms=1300,
            tool="hammer",
            action="strike",
            confidence=0.94,
            frame_hash="hammer-frame",
            candidate_step_index=4,
        ),
    ]

    observations = facts.build_observations(detections)

    assert len(observations) == 1
    assert observations[0].tool == "hammer"
    assert all(
        not (observation.object == "locking_nut" and observation.action == "strike")
        for observation in observations
    )


def test_build_observations_rejects_low_confidence_or_semantic_free_single_frame() -> None:
    low_confidence = facts.Detection(
        timestamp_ms=1000,
        object="locking_nut",
        action="loosen",
        confidence=0.84,
        frame_hash="low",
        candidate_step_index=4,
    )
    hand_present = facts.Detection(
        timestamp_ms=2000,
        hand="right",
        relation="hand_present",
        confidence=0.99,
        frame_hash="hand",
        candidate_step_index=4,
    )

    assert facts.build_observations([low_confidence, hand_present]) == []


def test_build_observations_rejects_action_only_short_candidate() -> None:
    action_only = facts.Detection(
        timestamp_ms=1000,
        action="loosen",
        relation="operates",
        confidence=0.99,
        frame_hash="action-only",
        candidate_step_index=4,
    )

    assert facts.build_observations([action_only]) == []


def test_candidate_index_does_not_fill_missing_step_semantics() -> None:
    steps = [
        SopStep(
            id=f"step-{index}",
            index=index,
            name=f"步骤 {index}",
            tools=[f"tool-{index}"],
            objects=[f"object-{index}"],
            actions=[f"action-{index}"],
            observable_checks=["工具、对象和动作关系可见"],
            expected_duration_s=5,
            weight=20,
        )
        for index in range(1, 6)
    ]
    observations = [
        facts.Observation(
            timestamp_ms=index * 1000,
            hand="right",
            tool="",
            object="",
            action=f"action-{index}",
            relation="operates",
            confidence=0.95,
            frame_hash=f"frame-{index}",
            candidate_step_index=index,
        )
        for index in range(1, 6)
    ]

    alignment = align.align_observations(steps, observations)
    scored = score_assessment(steps, alignment.step_results)

    assert alignment.matched_observation_count == 0
    assert all(result.judgement == "insufficient_evidence" for result in alignment.step_results)
    assert scored.evidence_coverage == 0.0
    assert scored.result == "inconclusive"


def test_full_short_semantic_evidence_can_pass_but_has_no_efficiency_score() -> None:
    step = SopStep(
        id="step-1",
        index=1,
        name="松开螺母",
        tools=["wrench"],
        objects=["locking_nut"],
        actions=["loosen"],
        observable_checks=["扳手正在松开锁紧螺母"],
        expected_duration_s=5,
        weight=100,
    )
    detections = [
        facts.Detection(
            timestamp_ms=1000,
            hand="right",
            tool="wrench",
            object="locking_nut",
            action="loosen",
            relation="right_operates_locking_nut_with_wrench",
            confidence=0.95,
            frame_hash="full-short",
            candidate_step_index=1,
        )
    ]

    observations = facts.build_observations(detections)
    alignment = align.align_observations([step], observations)
    scored = score_assessment([step], alignment.step_results)

    assert len(observations) == 1
    assert alignment.step_results[0].judgement == "passed"
    assert scored.dimension_scores["efficiency"] == 0.0


def test_action_evidence_cannot_implicitly_pass_critical_safety_check() -> None:
    """wrench/nut/tighten is not evidence that insulating gloves are worn."""
    step = SopStep(
        id="step-1",
        index=1,
        name="绝缘紧固",
        tools=["wrench"],
        objects=["nut"],
        actions=["tighten"],
        observable_checks=["绝缘手套已佩戴"],
        critical=True,
        expected_duration_s=5,
        weight=100,
    )
    observation = facts.Observation(
        timestamp_ms=1000,
        hand="right",
        tool="wrench",
        object="nut",
        action="tighten",
        relation="right_holds_wrench",
        confidence=0.95,
        frame_hash="ordinary-action-only",
        start_ms=1000,
        end_ms=2500,
        sample_count=2,
    )

    alignment = align.align_observations([step], [observation])
    scored = score_assessment([step], alignment.step_results)

    assert alignment.step_results[0].judgement == "passed"
    assert alignment.step_results[0].rule_code == "observable_checks_incomplete"
    assert alignment.step_results[0].observable_check_results == {
        "绝缘手套已佩戴": "insufficient_evidence"
    }
    assert scored.dimension_scores["observable_safety"] == 0.0
    assert scored.result == "inconclusive"
    assert any(d.rule_code == "observable_safety_insufficient" for d in scored.deductions)


def test_explicit_safety_violation_fails_step() -> None:
    step = SopStep(
        id="step-1",
        index=1,
        name="绝缘紧固",
        tools=["wrench"],
        objects=["nut"],
        actions=["tighten"],
        observable_checks=["绝缘手套已佩戴"],
        critical=True,
        weight=100,
    )
    observation = facts.Observation(
        timestamp_ms=1000,
        hand="right",
        tool="wrench",
        object="nut",
        action="tighten",
        relation="right_holds_wrench",
        confidence=0.95,
        frame_hash="visible-bare-hand",
        observable_check_results={"绝缘手套已佩戴": "failed"},
    )

    alignment = align.align_observations([step], [observation])
    scored = score_assessment([step], alignment.step_results)

    assert alignment.step_results[0].judgement == "failed"
    assert alignment.step_results[0].rule_code == "observable_check_failed"
    assert scored.dimension_scores["observable_safety"] == 0.0
    assert scored.result == "fail"


def test_low_confidence_observation_cannot_fail_or_pass_step() -> None:
    step = SopStep(
        id="step-1",
        index=1,
        name="松开螺母",
        tools=["wrench"],
        objects=["locking_nut"],
        actions=["loosen"],
        observable_checks=["扳手正在松开锁紧螺母"],
        weight=100,
    )
    observation = facts.Observation(
        timestamp_ms=1000,
        hand="right",
        tool="hammer",
        object="locking_nut",
        action="loosen",
        relation="operates",
        confidence=0.59,
        frame_hash="low-confidence",
        candidate_step_index=1,
    )

    result = align.align_observations([step], [observation]).step_results[0]

    assert result.judgement == "insufficient_evidence"


def test_fixture_detections_cover_all_steps() -> None:
    dets = fixture_detections(total_keyframes=24, scenario="pass")
    assert len(dets) == 24
    assert all(det.observable_check_results for det in dets)
    ok, reason = facts.quality_gate(dets, total_frames=24)
    assert ok is True, reason


def test_align_pass_all_steps() -> None:
    sop = grinder_disassembly_sop()
    dets = fixture_detections(total_keyframes=24, scenario="pass")
    obs = facts.build_observations(dets)
    alignment = align.align_observations(sop.steps, obs)
    passed = [r for r in alignment.step_results if r.judgement == "passed"]
    assert len(passed) == 7
    result = score_assessment(sop.steps, alignment.step_results)
    assert result.result == "pass"
    assert result.total_score >= 95.0


def test_align_missing_critical_step() -> None:
    """关键步无证据时不用“后续有证据”伪推断漏做。"""
    sop = grinder_disassembly_sop()
    dets = fixture_detections(total_keyframes=24, scenario="missing_step_1")
    obs = facts.build_observations(dets)
    alignment = align.align_observations(sop.steps, obs)
    step1 = next(r for r in alignment.step_results if r.step.index == 1)
    assert step1.judgement == "insufficient_evidence"
    assert step1.rule_code == "missing_evidence"
    result = score_assessment(sop.steps, alignment.step_results)
    assert result.result == "inconclusive"
    assert result.total_score > 59.0


def test_build_observations_merges_short_gap_but_splits_long_gap() -> None:
    dets = _detections([0.9, 0.9, 0.9, 0.9], ts_step=1000)
    dets[2].timestamp_ms = 9000
    dets[3].timestamp_ms = 10_000
    observations = facts.build_observations(dets)
    assert len(observations) == 2
    assert [(obs.start_ms, obs.end_ms) for obs in observations] == [
        (0, 1000),
        (9000, 10_000),
    ]


def test_build_observations_tolerates_one_missing_sample() -> None:
    dets = _detections([0.9, 0.9, 0.9], ts_step=1000)
    dets[2].timestamp_ms = 3000
    observations = facts.build_observations(dets)
    assert len(observations) == 1
    assert observations[0].sample_count == 3
    assert observations[0].end_ms == 3000


def test_align_marks_observed_wrong_order() -> None:
    sop = grinder_disassembly_sop()
    observations = [
        facts.Observation(
            timestamp_ms=5000,
            hand="right",
            tool="power_plug",
            object="power_outlet",
            action="unplug",
            relation="holds",
            confidence=0.9,
            frame_hash="later",
            observable_check_results={
                check: "passed" for check in sop.steps[0].observable_checks
            },
        ),
        facts.Observation(
            timestamp_ms=1000,
            hand="right",
            tool="",
            object="grinding_wheel",
            action="wait",
            relation="operates",
            confidence=0.9,
            frame_hash="earlier",
            observable_check_results={
                check: "passed" for check in sop.steps[1].observable_checks
            },
        ),
    ]
    result = align.align_observations(sop.steps[:2], observations)
    step2 = result.step_results[1]
    assert step2.judgement == "failed"
    assert step2.rule_code == "wrong_order"


def test_align_order_uses_earliest_match_not_later_display_keyframe() -> None:
    """A later high-confidence keyframe must not create a false order violation."""
    sop = grinder_disassembly_sop()
    observations = [
        facts.Observation(
            timestamp_ms=0,
            hand="right",
            tool="power_plug",
            object="power_outlet",
            action="unplug",
            relation="holds",
            confidence=0.7,
            frame_hash="step-1-earliest",
        ),
        facts.Observation(
            timestamp_ms=2000,
            hand="right",
            tool="",
            object="grinding_wheel",
            action="wait",
            relation="operates",
            confidence=0.9,
            frame_hash="step-2",
        ),
        facts.Observation(
            timestamp_ms=5000,
            hand="right",
            tool="power_plug",
            object="power_outlet",
            action="unplug",
            relation="holds",
            confidence=0.95,
            frame_hash="step-1-display",
        ),
    ]

    result = align.align_observations(sop.steps[:2], observations)

    step1, step2 = result.step_results
    assert step1.keyframe_ref == "step-1-display"
    assert step2.judgement == "passed"
    assert step2.rule_code != "wrong_order"


def test_align_wrong_tool_detected() -> None:
    sop = grinder_disassembly_sop()
    dets = fixture_detections(total_keyframes=24, scenario="wrong_tool_step_5")
    obs = facts.build_observations(dets)
    alignment = align.align_observations(sop.steps, obs)
    step5 = next(r for r in alignment.step_results if r.step.index == 5)
    assert step5.judgement == "failed"
    assert step5.rule_code == "wrong_tool"
    assert "hammer" in step5.reason
    assert step5.keyframe_ref


def test_unobservable_only_step() -> None:
    sop = grinder_disassembly_sop()
    step = sop.steps[0]
    unobservable_only = step.model_copy(update={"observable_checks": []})
    result = align.align_observations([unobservable_only], [])
    assert result.step_results[0].judgement == "unobservable"
    assert result.step_results[0].rule_code == "unobservable_only"


def test_recorded_pipeline_end_to_end_demo(db: Session, monkeypatch) -> None:
    """预录任务闭环（显式 demo 模式）：会话 → 任务 → fixture 观察 → 对齐 → 评分。

    demo 受 GONGXU_DEMO_FIXTURES 门控；来源标记 fixture。
    """
    monkeypatch.setenv("GONGXU_DEMO_FIXTURES", "true")
    from gongxu.config import get_settings

    get_settings.cache_clear()
    try:
        _recorded_pipeline_flow(db, demo=True)
    finally:
        get_settings.cache_clear()


def test_recorded_pipeline_fails_closed_without_models(db: Session) -> None:
    """正常模式（无真实模型）→ 结构化拒判，不产生数值分数（P0-B.3）。"""
    monkeypatch = __import__("pytest").MonkeyPatch()
    _recorded_pipeline_flow(db, demo=False, expect_reject=True)
    monkeypatch.undo()


def _recorded_pipeline_flow(db: Session, *, demo: bool, expect_reject: bool = False) -> None:
    from gongxu.assessment import service as assessment_service
    from gongxu.assets import service as asset_service
    from gongxu.db.models import UserRole
    from gongxu.jobs import service as jobs
    from gongxu.security.auth import create_user

    create_user(db, username="inst1", password="correct-horse-42", role=UserRole.instructor)
    sop = grinder_disassembly_sop()
    from gongxu.db.models import SopPublishStatus, SopVersion

    row = SopVersion(
        process_key=sop.process_key,
        version=1,
        status=SopPublishStatus.published,
        source_grade=sop.source_grade.value,
        steps=[s.model_dump(mode="json") for s in sop.steps],
        scoring_profile=sop.scoring_profile.model_dump(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # 正常模式需要真实视频资产（P0-B.1）；demo 路径忽略 input_asset_id
    input_asset_id = None
    if not demo:
        fake = Path(get_settings().data_root) / "runtime" / "input.mp4"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_bytes(b"\x00" * 100)
        input_asset_id = asset_service.register_asset(
            db, kind="video", filename="input.mp4", temp_path=fake, license_note="test"
        ).id

    session = assessment_service.create_session(
        db,
        process_key=sop.process_key,
        learner_id=None,
        sop_version_id=row.id,
        input_asset_id=input_asset_id,
    )
    job = jobs.enqueue(
        db,
        job_type="recorded_assessment",
        payload={"session_id": session.id},
        idempotency_key=f"assess:{session.id}",
    )
    work = Path(get_settings().data_root) / "runtime" / "w"
    summary = run_recorded_assessment_job(db, job.id, work_dir=work, demo=demo)
    if expect_reject:
        assert summary["rejected"] is True
        assert summary["status"] == "inconclusive"
        assert summary["reason_code"] == "model_unavailable"
        assert "hand" in summary["missing_models"]
        return
    assert summary["result"] == "pass"
    assert summary["total_score"] >= 95.0
    assert summary["observations"] >= 7
    assert summary["evidence_source"] == "fixture"

    stored_observations = (
        db.query(Observation)
        .filter(Observation.session_id == session.id)
        .order_by(Observation.timestamp_ms)
        .all()
    )
    assert len(stored_observations) == summary["observations"]
    assert all(
        observation.payload.get("observable_check_results")
        for observation in stored_observations
    )

    stored_steps = (
        db.query(StepAssessment)
        .filter(StepAssessment.session_id == session.id)
        .order_by(StepAssessment.step_index)
        .all()
    )
    assert len(stored_steps) == len(sop.steps)
    assert stored_steps[0].observable_check_results == {
        check: "passed" for check in sop.steps[0].observable_checks
    }
    assert summary["scoring_rules_version"] == assessment_service.SCORING_RULES_VERSION

    from fastapi.testclient import TestClient

    from gongxu.main import create_app

    client = TestClient(create_app())
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "inst1", "password": "correct-horse-42"},
    )
    assert login.status_code == 200
    report_response = client.get(f"/api/v1/assessment-sessions/{session.id}/report")
    assert report_response.status_code == 200
    assert report_response.json()["steps"][0]["observable_check_results"] == (
        stored_steps[0].observable_check_results
    )


def test_model_registry_marks_missing_and_requires(db: Session) -> None:
    """模型缺失时 require_model 明确拒判（§7 门禁），不猜测。"""
    from gongxu.vision import models as model_service

    model_service.ensure_required_registered(db)
    from gongxu.vision.models import ModelMissingError, require_model

    with pytest.raises(ModelMissingError, match="不可用"):
        require_model(db, "hand")

    # 登记后可用
    model_service.register_model(
        db, name="hand", kind="hand", version="v1", path=None, status="available", notes="fixture"
    )
    entry = require_model(db, "hand")
    assert entry.name == "hand"
