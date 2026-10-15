"""Contest replay scenarios must use the real deterministic scorer and stay honest."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gongxu.db.models import UserRole
from gongxu.demo.service import build_demo_scenario
from gongxu.main import create_app
from gongxu.security.auth import create_user


def test_demo_scenarios_have_distinct_expected_results() -> None:
    complete = build_demo_scenario("complete")
    error = build_demo_scenario("error")
    inconclusive = build_demo_scenario("inconclusive")

    assert complete["report"]["result"] == "pass"
    assert error["report"]["result"] == "fail"
    assert inconclusive["report"]["result"] == "inconclusive"
    assert complete["report"]["total_score"] > error["report"]["total_score"]
    assert all(step["observable_check_results"] for step in complete["steps"])
    assert all(
        set(step["observable_check_results"].values()) == {"passed"}
        for step in complete["steps"]
    )
    assert all(row["source"] == "fixture_replay" for row in (complete, error, inconclusive))
    assert all(
        "不代表真实模型准确率" in row["disclaimer"]
        for row in (complete, error, inconclusive)
    )


def test_demo_error_keeps_step_evidence() -> None:
    result = build_demo_scenario("error")
    failed = [step for step in result["steps"] if step["judgement"] == "failed"]

    assert failed
    assert failed[0]["rule_code"] == "wrong_tool"
    assert failed[0]["start_ms"] < failed[0]["end_ms"]


def test_demo_route_requires_auth_and_rejects_unknown(db) -> None:
    client = TestClient(create_app())
    assert client.post("/api/v1/demo/load/complete").status_code == 401

    create_user(
        db,
        username="learner-demo",
        password="correct-horse-42",
        role=UserRole.learner,
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "learner-demo", "password": "correct-horse-42"},
    )
    assert login.status_code == 200
    response = client.post("/api/v1/demo/load/complete")
    assert response.status_code == 200
    assert response.json()["cached"] is True
    assert client.post("/api/v1/demo/load/not-real").status_code == 404
