"""Progression safety and real API plumbing; model answers here are explicit test doubles."""

import base64
import io
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from gongxu.config import get_settings
from gongxu.db.models import (
    PracticeObservation,
    PracticeSession,
    SopPublishStatus,
    SopVersion,
    User,
    UserRole,
    utcnow,
)
from gongxu.main import create_app
from gongxu.practice import service, verifier
from gongxu.practice.reference import reference_images as real_reference_images
from gongxu.practice.schema import ObservationRequest
from gongxu.practice.state_machine import apply_verification, initial_state
from gongxu.practice_worker import run_once
from gongxu.providers.base import ProviderError, ProviderReceipt
from gongxu.security import auth
from gongxu.sop.schema import SopStep


def step(index=1, source="video", manual=False, critical=True):
    checks = [{"id": "direction", "criterion": "箭头朝前", "correction": "将箭头转向前端"}]
    if manual:
        checks.append({"id": "strength", "criterion": "连接强度已检查", "mode": "manual"})
    return SopStep(
        id=f"step_{index}",
        index=index,
        name="安装支架",
        weight=50,
        predecessors=[index - 1] if index > 1 else [],
        objects=["bracket"],
        actions=["install"],
        critical=critical,
        teaching={
            "source_asset_id": source,
            "start_ms": (index - 1) * 800,
            "end_ms": index * 800,
            "instruction": "将箭头朝前装入槽位",
        },
        verification={"checks": checks, "teacher_confirmed": True},
        evidence_refs=[
            {"kind": "video", "ref": "video@0ms", "asset_id": source, "timestamp_ms": 0}
        ],
    )


def metadata(sequence):
    return [{"id": f"frame_{sequence}", "sequence": sequence, "sha256": f"hash_{sequence}"}]


def burst_metadata(sequence):
    return [
        {
            "id": f"frame_{sequence + offset}",
            "sequence": sequence + offset,
            "captured_at_ms": offset * 500,
            "sha256": f"hash_{sequence + offset}",
        }
        for offset in range(3)
    ]


def checks(status="satisfied", sequence=1):
    return [
        {
            "check_id": "direction",
            "status": status,
            "evidence_frame_ids": [f"frame_{sequence}"]
            if status in ("satisfied", "violated")
            else [],
            "reason": "test observation",
        }
    ]


def test_confirm_new_evidence_then_advance_and_finish():
    steps = [step(), step(2)]
    state = initial_state(steps, 0)
    for seq in range(1, 5):
        state, event = apply_verification(
            state, steps, checks(sequence=seq), metadata(seq), seq * 100
        )
        if seq == 1:
            assert state["current_step_index"] == 1 and state["step_state"] == "confirming"
        if seq == 2:
            assert state["current_step_index"] == 2 and state["completed"] == [1]
    assert state["completed"] == [1, 2] and state["current_step_index"] is None
    assert event == "step_changed"


def test_ordinary_step_advances_after_one_temporal_burst():
    steps = [step(critical=False)]
    frames = burst_metadata(1)
    result = checks(sequence=1)
    result[0]["evidence_frame_ids"] = [frames[0]["id"], frames[-1]["id"]]
    state, event = apply_verification(initial_state(steps, 0), steps, result, frames, 1200)
    assert state["completed"] == [1]
    assert state["call_count"] == 0
    assert event == "step_changed"


def test_ordinary_step_waits_when_model_cites_only_one_frame():
    steps = [step(critical=False)]
    frames = burst_metadata(1)
    state, event = apply_verification(
        initial_state(steps, 0), steps, checks(sequence=1), frames, 1200
    )
    assert state["completed"] == []
    assert state["step_state"] == "confirming"
    assert "自动补拍" in state["feedback"]
    assert event == "verification_result"


@pytest.mark.parametrize("status", ["not_yet", "violated", "unknown"])
def test_no_advance_on_incomplete_wrong_or_occluded(status):
    steps = [step()]
    state = initial_state(steps, 0)
    for seq in (1, 2, 3):
        state, _ = apply_verification(state, steps, checks(status, seq), metadata(seq), 100)
    assert state["completed"] == [] and state["current_step_index"] == 1
    if status == "violated":
        assert state["feedback"] == "将箭头转向前端" and state["error_count"] == 1


def test_unknown_backoff_and_per_step_corrections_are_reportable():
    steps = [step(critical=False)]
    state = initial_state(steps, 0)
    state, _ = apply_verification(state, steps, checks("unknown"), metadata(1), 100)
    state, _ = apply_verification(state, steps, checks("unknown", 2), metadata(2), 200)
    assert state["unknown_streak"] == 2
    assert state["step_unknowns"] == {"1": 2}
    state, _ = apply_verification(state, steps, checks("violated", 3), metadata(3), 300)
    assert state["unknown_streak"] == 0
    assert state["step_errors"] == {"1": 1}


def test_repeated_image_cannot_confirm_and_error_can_be_corrected():
    steps = [step()]
    state, _ = apply_verification(
        initial_state(steps, 0), steps, checks("violated"), metadata(1), 100
    )
    state, _ = apply_verification(state, steps, checks(sequence=2), metadata(2), 200)
    repeated = [{"id": "frame_3", "sequence": 3, "sha256": "hash_2"}]
    state, _ = apply_verification(state, steps, checks(sequence=3), repeated, 300)
    assert state["completed"] == [] and state["confirm_count"] == 1
    state, _ = apply_verification(state, steps, checks(sequence=4), metadata(4), 400)
    assert state["completed"] == [1]


def test_manual_conditions_and_predecessors_are_required():
    steps = [step(manual=True)]
    state = initial_state(steps, 0)
    for seq in (1, 2):
        state, _ = apply_verification(state, steps, checks(sequence=seq), metadata(seq), 100)
    assert not state["completed"]
    state["manual_checks"] = ["strength"]
    for seq in (3, 4):
        state, _ = apply_verification(state, steps, checks(sequence=seq), metadata(seq), 100)
    assert state["completed"] == [1]
    state = initial_state([step(), step(2)], 0)
    state["current_step_index"] = 2
    for seq in (1, 2):
        state, _ = apply_verification(
            state, [step(), step(2)], checks(sequence=seq), metadata(seq), 100
        )
    assert not state["completed"]


def test_empty_checks_do_not_auto_complete():
    empty = step()
    empty.verification.checks = []
    state, _ = apply_verification(initial_state([empty], 0), [empty], [], metadata(1), 100)
    assert not state["completed"]


@pytest.mark.parametrize(
    "raw",
    [
        {"checks": [{"check_id": "invented", "status": "unknown"}]},
        {"checks": [{"check_id": "direction", "status": "satisfied", "evidence_frame_ids": []}]},
        {
            "checks": [
                {"check_id": "direction", "status": "satisfied", "evidence_frame_ids": ["foreign"]}
            ]
        },
        {"checks": checks() * 2},
        {"checks": checks(), "advance": True},
        {"checks": "yes"},
    ],
)
def test_invalid_or_fabricated_model_evidence_is_rejected(raw):
    with pytest.raises(ProviderError):
        verifier.validate_result(raw, step(), metadata(1))


def test_missing_condition_is_unknown():
    result = verifier.validate_result({"checks": []}, step(), metadata(1))
    assert result[0]["status"] == "unknown"


@pytest.fixture
def practice(db, video_asset, monkeypatch):
    for key, value in {
        "GONGXU_LLM_MODE": "live",
        "GONGXU_LLM_BASE_URL": "https://model.invalid/v1",
        "GONGXU_LLM_API_KEY": "test-double",
        "GONGXU_LLM_MODEL": "test-vision",
        "GONGXU_PRACTICE_WORKER_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    learner = User(username="practice_learner", role=UserRole.learner, password_hash="unused")
    db.add(learner)
    sop = SopVersion(
        process_key="practice_test",
        status=SopPublishStatus.published,
        steps=[s.model_dump(mode="json") for s in [step(source=video_asset), step(2, video_asset)]],
        reference_asset_id=video_asset,
        source_asset_ids=[video_asset],
    )
    db.add(sop)
    db.commit()
    row = service.create(db, learner, sop.id, True)
    monkeypatch.setattr("gongxu.practice.reference.reference_images", lambda *_: ["test-standard"])
    return row, learner, sop


def request(row, sequence=1, key=None):
    images = []
    for seq in (sequence, sequence + 1, sequence + 2):
        output = io.BytesIO()
        Image.new("RGB", (96, 96), (seq * 17 % 255, 70, 90)).save(output, format="JPEG")
        images.append(
            {
                "sequence": seq,
                "captured_at_ms": service.now_ms() + (seq - sequence) * 500,
                "data_uri": "data:image/jpeg;base64,"
                + base64.b64encode(output.getvalue()).decode(),
            }
        )
    return ObservationRequest(
        request_key=key or f"request_{sequence}",
        step_revision=row.state["step_revision"],
        frames=images,
    )


def fake_verify(step, frames, _refs):
    return checks(sequence=frames[-1]["sequence"]), ProviderReceipt(
        provider="test-double",
        mode="live",
        model_id="test-vision",
        prompt_version="test",
        input_digest="test-input",
        response_digest="test-output",
        latency_ms=1,
    )


def test_submission_is_idempotent_and_only_one_is_inflight(db, practice):
    row, _, _ = practice
    body = request(row)
    observation = service.submit(db, row, body)
    assert service.submit(db, row, body).id == observation.id
    with pytest.raises(HTTPException, match="上一轮"):
        service.submit(db, row, request(row, 3))
    assert row.state["call_count"] == 1


def test_worker_records_evidence_and_advances_with_new_frames(db, practice, monkeypatch):
    row, _, _ = practice
    monkeypatch.setattr(verifier, "verify_step", fake_verify)
    for seq in (1, 4, 7, 10):
        db.refresh(row)
        service.submit(db, row, request(row, seq))
        assert run_once()
    db.refresh(row)
    assert row.status == "finished" and row.state["completed"] == [1, 2]
    observations = db.scalars(
        select(PracticeObservation).where(PracticeObservation.session_id == row.id)
    ).all()
    assert all(obs.status == "completed" and obs.receipt_id for obs in observations)


def test_paused_session_ignores_late_result(db, test_db, practice, monkeypatch):
    row, learner, _ = practice
    observation = service.submit(db, row, request(row))

    def pause_during_request(*args):
        with test_db() as other:
            current = other.get(PracticeSession, row.id)
            service.action(other, current, learner, "pause", [], "")
        return fake_verify(*args)

    monkeypatch.setattr(verifier, "verify_step", pause_during_request)
    assert run_once()
    db.refresh(row)
    db.refresh(observation)
    assert row.status == "paused" and not row.state["completed"]
    assert observation.status == "discarded" and observation.encrypted_frames is None


def test_expired_and_failed_requests_release_pending_without_passing(db, practice, monkeypatch):
    row, _, _ = practice
    obs = service.submit(db, row, request(row))
    obs.created_at -= timedelta(seconds=90)
    db.commit()
    assert run_once()
    db.refresh(row)
    assert row.state["pending_observation_id"] is None and not row.state["completed"]
    monkeypatch.setattr(
        verifier, "verify_step", lambda *_: (_ for _ in ()).throw(ProviderError("test outage"))
    )
    service.submit(db, row, request(row, 4))
    assert run_once()
    db.refresh(row)
    assert row.state["pending_observation_id"] is None and not row.state["completed"]
    assert row.state["service_error"]


def test_camera_consent_credentials_and_freshness_are_enforced(db, practice, monkeypatch):
    row, user, sop = practice
    with pytest.raises(HTTPException):
        service.create(db, user, sop.id, False)
    stale = request(row)
    stale.frames[0].captured_at_ms = row.state["entered_at_ms"] - 1
    with pytest.raises(HTTPException, match="过期"):
        service.submit(db, row, stale)
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as exc:
        service.submit(db, row, request(row))
    assert exc.value.status_code == 503


def test_teacher_confirmation_and_required_steps_cannot_be_bypassed(db, practice):
    row, user, sop = practice
    with pytest.raises(HTTPException):
        service.action(db, row, user, "skip_optional", [], "")
    with pytest.raises(HTTPException):
        service.action(db, row, user, "confirm_manual", ["strength"], "looks fine")
    changed = deepcopy(sop.steps)
    changed[0]["verification"]["teacher_confirmed"] = False
    sop.steps = changed
    db.commit()
    with pytest.raises(HTTPException, match="校对"):
        service.create(db, user, sop.id, True)


def test_api_authorization_snapshot_and_event_cursor(db, practice):
    row, user, _ = practice
    app = create_app()
    app.dependency_overrides[auth.get_current_user] = lambda: user
    client = TestClient(app)
    assert (
        client.get(f"/api/v1/practice-sessions/{row.id}").json()["state"]["current_step_index"] == 1
    )
    result = client.post(
        f"/api/v1/practice-sessions/{row.id}/observations", json=request(row).model_dump()
    )
    assert result.status_code == 202
    from gongxu.api.routes.practice import _events_since

    events = _events_since(row.id, 0)
    assert events and _events_since(row.id, events[-1]["id"]) == []
    stranger = User(id="another_user", username="stranger", role=UserRole.learner)
    app.dependency_overrides[auth.get_current_user] = lambda: stranger
    assert client.get(f"/api/v1/practice-sessions/{row.id}").status_code == 403


def test_real_reference_frames_are_extractable(db, practice, monkeypatch):
    row, _, _ = practice
    images = real_reference_images(db, SopStep.model_validate(row.steps[0]))
    assert len(images) == 2 and all(image.startswith("data:image/jpeg;base64,") for image in images)
    monkeypatch.setattr(
        "gongxu.practice.reference.decrypt_to_temp",
        lambda *a, **kw: pytest.fail("A cached standard must not decrypt the full video again"),
    )
    assert real_reference_images(db, SopStep.model_validate(row.steps[0])) == images


def test_verifier_sends_ordered_frames_and_bounded_cloud_call(db, practice, monkeypatch):
    import json

    from gongxu.providers.qwen import Qwen38MaxProvider, _ChatMetadata

    row, _, _ = practice
    actual_step = SopStep.model_validate(row.steps[0])
    frames = [
        {**f, "captured_at_ms": i * 500, "data_uri": f"camera-{i}"}
        for i, f in enumerate(metadata(1) + metadata(2))
    ]
    captured = {}

    def chat_double(self, messages, **kwargs):
        captured.update(kwargs)
        content = messages[0]["content"]
        prompt = json.loads(content[0]["text"])
        assert [f["id"] for f in prompt["frames"]] == ["frame_1", "frame_2"]
        assert [c["image_url"]["url"] for c in content if c["type"] == "image_url"] == [
            "standard-before",
            "standard-after",
            "camera-0",
            "camera-1",
        ]
        return {"checks": checks(sequence=2)}, _ChatMetadata(input_tokens=20, output_tokens=10)

    monkeypatch.setattr(Qwen38MaxProvider, "_chat_with_metadata", chat_double)
    result, receipt = verifier.verify_step(
        actual_step, frames, ["standard-before", "standard-after"]
    )
    assert result[0]["evidence_frame_ids"] == ["frame_2"]
    assert captured["attempts"] == 1 and captured["timeout_seconds"] == 25
    assert receipt.input_tokens == 20 and receipt.output_tokens == 10


def test_verifier_works_with_gateway_requiring_explicit_json(db, practice, monkeypatch):
    import json

    import httpx

    row, _, _ = practice
    actual_step = SopStep.model_validate(row.steps[0])
    frames = [
        {**f, "captured_at_ms": i * 500, "data_uri": f"camera-{i}"}
        for i, f in enumerate(metadata(1) + metadata(2))
    ]
    json_module = json

    def gateway(url, *, json, **kwargs):
        assert json["response_format"] == {"type": "json_object"}
        text = json["messages"][0]["content"][0]["text"]
        if "json" not in text.lower():
            return httpx.Response(
                400,
                json={"error": "messages must contain the word json"},
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json_module.dumps({"checks": checks()})}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("gongxu.providers.qwen.httpx.post", gateway)
    result, receipt = verifier.verify_step(actual_step, frames, ["standard-before"])
    assert result[0]["status"] == "satisfied"
    assert result[0]["evidence_frame_ids"] == ["frame_1"]
    assert receipt.mode == "live"


def test_page_recovery_expires_pending_even_if_worker_is_unavailable(db, practice):
    row, user, _ = practice
    observation = service.submit(db, row, request(row))
    observation.created_at = utcnow() - timedelta(seconds=60)
    db.commit()
    app = create_app()
    app.dependency_overrides[auth.get_current_user] = lambda: user
    restored = TestClient(app).get(f"/api/v1/practice-sessions/{row.id}").json()
    assert restored["state"]["pending_observation_id"] is None
    assert not restored["state"]["completed"] and restored["state"]["service_error"]
    db.refresh(observation)
    assert observation.status == "discarded" and observation.encrypted_frames is None


def test_pause_excludes_idle_time_from_step_duration(db, practice, monkeypatch):
    row, user, _ = practice
    entered = row.state["entered_at_ms"]
    monkeypatch.setattr(service, "now_ms", lambda: entered + 1000)
    service.action(db, row, user, "pause", [], "")
    monkeypatch.setattr(service, "now_ms", lambda: entered + 10_000)
    service.action(db, row, user, "resume", [], "")
    steps = [SopStep.model_validate(s) for s in row.steps]
    result, _ = apply_verification(row.state, steps, checks(), metadata(1), entered + 11_000)
    result, _ = apply_verification(result, steps, checks(sequence=2), metadata(2), entered + 12_000)
    assert result["attempts"][0]["duration_ms"] == 3000


def test_course_catalog_keeps_latest_version_and_exposes_metadata(db, practice):
    _, user, original = practice
    original.course_meta = {"title": "旧课程"}
    newer = SopVersion(
        process_key=original.process_key,
        version=2,
        status=SopPublishStatus.published,
        steps=deepcopy(original.steps),
        reference_asset_id=original.reference_asset_id,
        source_asset_ids=list(original.source_asset_ids),
        course_meta={
            "title": "新版课程",
            "description": "动态课程信息",
            "objectives": ["完成全部步骤"],
            "difficulty": "入门",
            "audience": "零基础学习者",
            "estimated_minutes": 8,
        },
    )
    db.add(newer)
    db.commit()
    app = create_app()
    app.dependency_overrides[auth.get_current_user] = lambda: user
    courses = TestClient(app).get("/api/v1/practice-sessions/courses").json()["courses"]
    matched = [item for item in courses if item["process_key"] == original.process_key]
    assert len(matched) == 1
    assert matched[0]["version"] == 2
    assert matched[0]["course_meta"]["title"] == "新版课程"


def test_teacher_analytics_and_student_report_are_role_scoped(db, practice):
    row, learner, _ = practice
    state = deepcopy(row.state)
    state.update(
        completed=[1],
        error_count=1,
        step_errors={"1": 1},
        step_unknowns={"1": 2},
        attempts=[{"step_index": 1, "duration_ms": 95_000, "source": "visual_verified"}],
    )
    row.state = state
    db.commit()
    teacher = User(username="analytics_teacher", role=UserRole.instructor, password_hash="unused")
    outsider = User(username="analytics_outsider", role=UserRole.learner, password_hash="unused")
    db.add_all([teacher, outsider])
    db.commit()
    app = create_app()
    client = TestClient(app)
    app.dependency_overrides[auth.get_current_user] = lambda: teacher
    analytics = client.get("/api/v1/practice-sessions/analytics")
    assert analytics.status_code == 200
    payload = analytics.json()
    assert payload["summary"]["learner_count"] == 1
    assert payload["weak_steps"][0]["name"] == "安装支架"
    assert payload["sessions"][0]["learner_name"] == learner.username
    app.dependency_overrides[auth.get_current_user] = lambda: outsider
    assert client.get("/api/v1/practice-sessions").json() == []
    app.dependency_overrides[auth.get_current_user] = lambda: learner
    report = client.get(f"/api/v1/practice-sessions/{row.id}/report").json()
    assert report["completion_rate"] == 50
    assert report["weak_steps"][0]["error_count"] == 1
