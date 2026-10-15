"""任务队列测试：幂等、租约、重试、心跳与恢复。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient

from gongxu.db.models import Job, JobStatus, UserRole, utcnow
from gongxu.jobs import service as jobs
from gongxu.main import create_app
from gongxu.security.auth import create_user


def test_enqueue_and_claim(db) -> None:
    job = jobs.enqueue(
        db, job_type="asset_process", payload={"asset_id": "a1"}, idempotency_key="k1"
    )
    assert job.status == JobStatus.queued

    claimed = jobs.claim_next(db, worker_name="w1")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == JobStatus.running
    assert claimed.lease_owner == "w1"

    # 已领取的任务不能被同一 worker 再次领取
    assert jobs.claim_next(db, worker_name="w1") is None


def test_idempotency_key_dedup(db) -> None:
    first = jobs.enqueue(db, job_type="asset_process", payload={}, idempotency_key="same-key")
    second = jobs.enqueue(db, job_type="asset_process", payload={}, idempotency_key="same-key")
    assert first.id == second.id


def test_claim_expired_lease_recoverable(db) -> None:
    job = jobs.enqueue(db, job_type="t", payload={}, idempotency_key="k")
    jobs.claim_next(db, worker_name="w1")

    # 模拟 worker 崩溃：租约过期
    now = utcnow()
    db.get(Job, job.id).lease_expires_at = now - timedelta(seconds=1)
    db.commit()

    # 新 worker 可重新领取
    recovered = jobs.claim_next(db, worker_name="w2")
    assert recovered is not None
    assert recovered.lease_owner == "w2"


def test_claim_next_is_atomic_across_competing_workers(test_db, monkeypatch) -> None:
    """Both workers observe one snapshot, but only one CAS may acquire its lease."""
    setup_db = test_db()
    try:
        queued = jobs.enqueue(
            setup_db,
            job_type="asset_process",
            payload={"asset_id": "atomic"},
            idempotency_key="atomic-claim",
        )
        job_id = queued.id
    finally:
        setup_db.close()

    original_candidates = jobs._claim_candidates
    both_observed = threading.Barrier(2)

    def synchronized_candidates(db, *, now, job_types):
        candidates = original_candidates(db, now=now, job_types=job_types)
        assert [candidate.job_id for candidate in candidates] == [job_id]
        both_observed.wait(timeout=5)
        return candidates

    monkeypatch.setattr(jobs, "_claim_candidates", synchronized_candidates)

    def claim(worker_name: str):
        worker_db = test_db()
        try:
            return jobs.claim_next(worker_db, worker_name=worker_name)
        finally:
            worker_db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ("worker-a", "worker-b")))

    winners = [job for job in claims if job is not None]
    assert len(winners) == 1
    assert winners[0].id == job_id
    assert winners[0].lease_owner in {"worker-a", "worker-b"}

    verify_db = test_db()
    try:
        persisted = verify_db.get(Job, job_id)
        assert persisted is not None
        assert persisted.status == JobStatus.running
        assert persisted.lease_owner == winners[0].lease_owner
        assert persisted.heartbeat_at == winners[0].heartbeat_at
        assert persisted.lease_expires_at == winners[0].lease_expires_at
    finally:
        verify_db.close()


def test_complete_and_fail(db) -> None:
    job = jobs.enqueue(db, job_type="t", payload={}, idempotency_key="k-c")
    jobs.claim_next(db, worker_name="w1")
    done = jobs.complete(db, job.id, result={"ok": True})
    assert done.status == JobStatus.succeeded
    assert done.result == {"ok": True}

    job2 = jobs.enqueue(db, job_type="t", payload={}, idempotency_key="k-f")
    jobs.claim_next(db, worker_name="w1")
    failed = jobs.fail(db, job2.id, error="boom")
    # max_retries=3，第一次失败进入 retrying
    assert failed.status == JobStatus.retrying
    assert failed.retry_count == 1

    jobs.claim_next(db, worker_name="w1")
    jobs.fail(db, job2.id, error="boom")
    jobs.claim_next(db, worker_name="w1")
    jobs.fail(db, job2.id, error="boom")
    jobs.claim_next(db, worker_name="w1")
    final = jobs.fail(db, job2.id, error="boom")
    # 第 4 次失败超过 max_retries=3 → failed
    assert final.status == JobStatus.failed
    assert final.retry_count == 4


def test_heartbeat_extends_lease(db) -> None:
    job = jobs.enqueue(db, job_type="t", payload={}, idempotency_key="k-hb")
    jobs.claim_next(db, worker_name="w1")
    jobs.heartbeat(db, job.id, lease_seconds=600)
    job = db.get(Job, job.id)
    assert job.lease_expires_at is not None
    assert job.heartbeat_at is not None


def test_save_checkpoint(db) -> None:
    job = jobs.enqueue(db, job_type="t", payload={}, idempotency_key="k-ck")
    jobs.claim_next(db, worker_name="w1")
    jobs.save_checkpoint(db, job.id, {"stage": "transcode", "progress": 0.5})
    job = db.get(Job, job.id)
    assert job.checkpoint == {"stage": "transcode", "progress": 0.5}


def test_cancel(db) -> None:
    job = jobs.enqueue(db, job_type="t", payload={}, idempotency_key="k-x")
    cancelled = jobs.cancel(db, job.id)
    assert cancelled.status == JobStatus.cancelled
    # 已取消任务不可再领取
    assert jobs.claim_next(db, worker_name="w1") is None


def test_progress_and_manual_retry_are_queryable(db) -> None:
    owner = create_user(
        db,
        username="job_owner",
        password="correct-horse-42",
        role=UserRole.instructor,
    )
    client = TestClient(create_app())
    assert client.post(
        "/api/v1/auth/login",
        json={"username": "job_owner", "password": "correct-horse-42"},
    ).status_code == 200

    job = jobs.enqueue(
        db,
        job_type="sop_compile",
        payload={"requested_by": owner.id},
        idempotency_key="manual-retry",
        max_retries=0,
    )
    jobs.claim_next(db, worker_name="w1")
    jobs.set_progress(
        db,
        job.id,
        stage="sampling",
        progress_percent=35,
        status_text="正在抽帧",
        details={"sampled_frames": 8},
    )
    failed = jobs.fail(db, job.id, error="provider timeout")
    assert failed.status == JobStatus.failed

    progress = client.get(f"/api/v1/jobs/{job.id}")
    assert progress.status_code == 200
    assert progress.json()["stage"] == "failed"
    assert progress.json()["checkpoint"]["sampled_frames"] == 8
    retried = client.post(f"/api/v1/jobs/{job.id}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    assert retried.json()["progress_percent"] == 0
