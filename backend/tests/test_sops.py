"""SOP API 测试：Replay 编译、非法输出拒入库、发布与权限。"""

from __future__ import annotations

import copy
import io
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from gongxu.db.models import UserRole
from gongxu.main import create_app
from gongxu.providers.base import ProviderReceipt
from gongxu.security.auth import create_user


class _BadProvider:
    """返回非法 SOP 的假 Provider：权重总和不为 100。"""

    name = "bad"
    mode = "replay"

    def analyze_reference_segment(self, segment):
        receipt = ProviderReceipt(
            provider="bad",
            mode="replay",
            model_id="x",
            prompt_version="v1",
            input_digest="a",
            response_digest="b",
        )
        return {"steps_found": 1}, receipt

    def compile_sop(self, analysis, rag_chunks):
        receipt = ProviderReceipt(
            provider="bad",
            mode="replay",
            model_id="x",
            prompt_version="v1",
            input_digest="a",
            response_digest="b",
        )
        return (
            {
                "process_key": "impact_grinder_safe_subassembly",
                "version": 1,
                "source_grade": "video_reference",
                "steps": [{"index": 1, "name": "s1", "weight": 10}],  # 缺字段+权重错误
            },
            receipt,
        )


def _make_user(db, *, role: UserRole, username: str):
    return create_user(db, username=username, password="correct-horse-42", role=role)


def _login(db, username: str) -> TestClient:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "correct-horse-42"}
    )
    assert resp.status_code == 200
    return client


def _inject_explicit_replay(monkeypatch) -> None:
    """Use Replay only as an explicitly labelled test double."""
    from gongxu.providers.replay import ReplayProvider

    monkeypatch.setattr("gongxu.api.routes.sops.get_provider", lambda: ReplayProvider())
    monkeypatch.setattr(
        "gongxu.providers.factory.get_provider",
        lambda *args, **kwargs: ReplayProvider(),
    )


def test_compile_sop_with_explicit_replay_test_double(db, video_asset, monkeypatch) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_user(db, role=UserRole.instructor, username="inst1")
    client = _login(db, "inst1")
    resp = client.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"
    assert len(body["steps"]) == 7
    assert body["source_grade"] == "video_reference"


def test_normal_compile_fails_closed_in_replay_mode(db, video_asset) -> None:
    _make_user(db, role=UserRole.instructor, username="no_provider")
    client = _login(db, "no_provider")

    response = client.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert response.status_code == 503
    assert "replay 工程回放模式" in response.json()["detail"]

    queued = client.post(
        "/api/v1/sops/compile-jobs",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert queued.status_code == 503


def test_compile_sop_samples_full_video_and_preserves_timestamps(
    db, video_asset, monkeypatch
) -> None:
    """标准视频编译使用全片均匀帧，并把真实时间轴传到 Provider/编译阶段。"""
    from gongxu.media import video as video_module
    from gongxu.providers.replay import ReplayProvider
    from gongxu.sop import service as sop_service

    captured: dict = {}

    def _sample_uniform(src, dst_dir, *, count):
        assert count == 24
        dst_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for index, timestamp_ms in enumerate((0, 45_000, 90_000)):
            path = dst_dir / f"kf_{index:04d}.jpg"
            path.write_bytes(f"frame-{index}".encode())
            result.append(
                video_module.SampledFrame(
                    path=path,
                    frame_index=index,
                    timestamp_ms=timestamp_ms,
                )
            )
        return result

    class _CapturingReplay(ReplayProvider):
        def analyze_reference_segment(self, segment):
            captured["segment"] = segment
            analysis, receipt = super().analyze_reference_segment(segment)
            captured["analysis"] = analysis
            return analysis, receipt

        def compile_sop(self, analysis, rag_chunks):
            captured["compiled_analysis"] = analysis
            return super().compile_sop(analysis, rag_chunks)

    monkeypatch.setattr(video_module, "extract_keyframes_uniform", _sample_uniform)
    sop_service.compile_sop_from_media(
        db,
        provider=_CapturingReplay(),
        process_key="impact_grinder_safe_subassembly",
        reference_video_asset_id=video_asset,
        manual_asset_ids=[],
    )

    segment = captured["segment"]
    assert segment.keyframe_timestamps_ms == [0, 45_000, 90_000]
    assert segment.start_ms == 0
    assert segment.end_ms == 90_000
    assert captured["compiled_analysis"]["reference_timeline"] == [
        {"frame_index": 0, "timestamp_ms": 0},
        {"frame_index": 1, "timestamp_ms": 45_000},
        {"frame_index": 2, "timestamp_ms": 90_000},
    ]


def test_compile_rejects_without_provider_export_consent(db, video_asset, monkeypatch) -> None:
    """标准视频未授权外部 AI 分析时拒绝编译（整改 P1-1 出站同意）。"""
    _inject_explicit_replay(monkeypatch)
    _make_user(db, role=UserRole.instructor, username="inst1")
    client = _login(db, "inst1")
    # 将视频资产改为未授权外发
    from gongxu.db.models import SourceAsset

    v = db.get(SourceAsset, video_asset)
    v.allow_provider_export = False
    db.commit()
    resp = client.post(
        "/api/v1/sops/compile",
        json={"process_key": "p", "reference_video_asset_id": video_asset, "manual_asset_ids": []},
    )
    assert resp.status_code == 422
    assert "未授权" in resp.json()["detail"]


def test_compile_rejects_unindexed_manual(db, video_asset, monkeypatch) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_user(db, role=UserRole.instructor, username="manual_inst")
    client = _login(db, "manual_inst")
    uploaded = client.post(
        "/api/v1/assets",
        files={
            "file": (
                "manual.txt",
                io.BytesIO("断电后再拆卸".encode()),
                "text/plain",
            )
        },
        data={
            "kind": "manual",
            "license_note": "测试授权",
            "allow_provider_export": "true",
        },
    )
    assert uploaded.status_code == 201

    response = client.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [uploaded.json()["id"]],
        },
    )
    assert response.status_code == 422
    assert "尚未处理并建立 RAG 索引" in response.json()["detail"]


def test_invalid_provider_output_not_persisted(db, monkeypatch, video_asset) -> None:
    """非法大模型输出不能入库（handoff §8）。"""
    _make_user(db, role=UserRole.instructor, username="inst2")
    client = _login(db, "inst2")
    monkeypatch.setattr("gongxu.api.routes.sops.get_provider", lambda: _BadProvider())
    resp = client.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert resp.status_code == 422
    # 没有创建任何 SOP 版本
    from gongxu.db.models import SopVersion

    assert db.query(SopVersion).count() == 0


def test_publish_flow(db, video_asset, monkeypatch) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_user(db, role=UserRole.instructor, username="inst3")
    client = _login(db, "inst3")
    resp = client.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert resp.status_code == 201
    sop_id = resp.json()["id"]

    pub = client.post(f"/api/v1/sops/{sop_id}/publish")
    assert pub.status_code == 200
    assert pub.json()["status"] == "published"

    # 重复发布 → 409
    again = client.post(f"/api/v1/sops/{sop_id}/publish")
    assert again.status_code == 409


def _draft_with_real_video_source(db, video_asset):
    from gongxu.db.models import (
        SopPublishStatus,
        SopVersion,
        SourceGrade,
    )
    from gongxu.sop.fixtures import grinder_disassembly_sop

    fixture = grinder_disassembly_sop()
    row = SopVersion(
        process_key=fixture.process_key,
        version=1,
        status=SopPublishStatus.draft,
        source_grade=SourceGrade.video_reference,
        steps=[step.model_dump(mode="json") for step in fixture.steps],
        scoring_profile=fixture.scoring_profile.model_dump(),
        prompt_version=fixture.prompt_version,
        reference_asset_id=video_asset,
        source_asset_ids=[video_asset],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_publish_revalidates_full_pydantic_schema(db, video_asset) -> None:
    from gongxu.sop.service import SopPublishError, publish_sop

    row = _draft_with_real_video_source(db, video_asset)
    broken_steps = copy.deepcopy(row.steps)
    del broken_steps[0]["name"]
    row.steps = broken_steps
    db.commit()

    with pytest.raises(SopPublishError, match="完整校验失败"):
        publish_sop(db, row.id)

    assert row.status.value == "draft"


def test_publish_revalidates_business_rules(db, video_asset) -> None:
    from gongxu.sop.service import SopPublishError, publish_sop

    row = _draft_with_real_video_source(db, video_asset)
    broken_steps = copy.deepcopy(row.steps)
    broken_steps[0]["weight"] = 1
    row.steps = broken_steps
    db.commit()

    with pytest.raises(SopPublishError, match="权重总和"):
        publish_sop(db, row.id)

    assert row.status.value == "draft"


def test_publish_rejects_nonexistent_source_asset(db, video_asset) -> None:
    from gongxu.sop.service import SopPublishError, publish_sop

    row = _draft_with_real_video_source(db, video_asset)
    row.source_asset_ids = [video_asset, "missing-source-asset"]
    db.commit()

    with pytest.raises(SopPublishError, match="来源资产不存在"):
        publish_sop(db, row.id)


def test_publish_rejects_source_asset_without_encrypted_file(db, video_asset) -> None:
    from gongxu.db.models import SourceAsset
    from gongxu.sop.service import SopPublishError, publish_sop

    row = _draft_with_real_video_source(db, video_asset)
    video = db.get(SourceAsset, video_asset)
    video.encrypted_path = None
    db.commit()

    with pytest.raises(SopPublishError, match="来源资产密文不可用"):
        publish_sop(db, row.id)


def test_manual_backed_publish_requires_linked_real_manual_asset(db, video_asset, tmp_path) -> None:
    from gongxu.assets.indexing import get_chunk_provenance, index_document_asset
    from gongxu.assets.service import register_asset
    from gongxu.db.models import DocumentChunk, SourceGrade
    from gongxu.sop.service import SopPublishError, publish_sop

    row = _draft_with_real_video_source(db, video_asset)
    steps = copy.deepcopy(row.steps)
    steps[0]["evidence_refs"] = [{"kind": "manual", "ref": "手册p.12", "description": "断电要求"}]
    row.steps = steps
    row.source_grade = SourceGrade.manual_backed
    db.commit()

    with pytest.raises(SopPublishError, match="未关联真实 manual 资产"):
        publish_sop(db, row.id)

    manual_path = tmp_path / "manual.txt"
    manual_path.write_text("操作前必须断开电源。", encoding="utf-8")
    manual = register_asset(
        db,
        kind="manual",
        filename="manual.txt",
        temp_path=manual_path,
        license_note="测试授权",
    )
    index_document_asset(db, manual.id, work_dir=tmp_path / "index")
    chunk = db.query(DocumentChunk).filter(DocumentChunk.asset_id == manual.id).one()
    provenance = get_chunk_provenance(db, chunk.id)
    assert provenance is not None
    steps[0]["evidence_refs"] = [
        {
            "kind": "manual",
            "ref": f"manual@{manual.id}#chunk={chunk.id}",
            "description": "断电要求",
            **provenance,
        }
    ]
    row.steps = steps
    row.source_asset_ids = [video_asset, manual.id]
    db.commit()

    published = publish_sop(db, row.id)
    assert published.status.value == "published"


def test_compile_adapter_preserves_manual_asset_page_section_and_hash() -> None:
    from gongxu.sop import service as sop_service

    rag_chunk = {
        "chunk_id": 17,
        "asset_id": "manual-asset-1",
        "chunk_index": 2,
        "content_hash": "a" * 64,
        "page": 12,
        "section": "3.2 断电操作",
        "text": "操作前必须断开电源。",
        "score": 1.0,
    }
    converted = sop_service._analysis_to_sop(  # noqa: SLF001
        {
            "steps": [
                {
                    "index": 1,
                    "name": "断开电源",
                    "tools": ["power_plug"],
                    "objects": ["power_outlet"],
                    "actions": ["unplug"],
                    "observable_checks": ["插头已拔出"],
                    "unobservable_checks": [],
                    "weight": 100,
                    "evidence_refs": [
                        {"kind": "video", "ref": "video@1000ms"},
                        {"kind": "manual", "chunk_id": 17, "ref": "rag@17"},
                    ],
                }
            ]
        },
        {"reference_timeline": [{"frame_index": 1, "timestamp_ms": 1000}]},
        "impact_grinder_safe_subassembly",
        [],
        reference_video_asset_id="video-asset-1",
        rag_chunks=[rag_chunk],
    )
    assert converted["source_grade"] == "manual_backed"
    assert converted["steps"][0]["evidence_refs"][1] == {
        "kind": "manual",
        "ref": "manual@manual-asset-1#chunk=17;page=12;section=3.2 断电操作",
        "description": "手册 RAG 来源片段",
        "asset_id": "manual-asset-1",
        "page": 12,
        "section": "3.2 断电操作",
        "chunk_id": 17,
        "chunk_index": 2,
        "content_hash": "a" * 64,
    }


def test_learner_cannot_compile(db) -> None:
    _make_user(db, role=UserRole.learner, username="learner_x")
    client = _login(db, "learner_x")
    resp = client.post(
        "/api/v1/sops/compile",
        json={"process_key": "p", "reference_video_asset_id": "x", "manual_asset_ids": []},
    )
    assert resp.status_code == 403


def test_observer_can_read_sops(db, video_asset, monkeypatch) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_user(db, role=UserRole.instructor, username="inst4")
    client = _login(db, "inst4")
    resp = client.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert resp.status_code == 201

    _make_user(db, role=UserRole.observer, username="obs1")
    obs = _login(db, "obs1")
    listing = obs.get("/api/v1/sops")
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_compile_injects_controlled_vocab(db, video_asset) -> None:
    """compile_sop 收到注入受控词表的 analysis → 输出可发布（修复①：编译闭环）。"""
    from gongxu.providers.base import ProviderReceipt
    from gongxu.sop import service as sop_service

    captured: dict = {}

    class _VocabProvider:
        name = "vocabtest"
        mode = "live"

        def analyze_reference_segment(self, segment):
            return {"steps_found": 1}, ProviderReceipt(
                provider="v",
                mode="live",
                model_id="m",
                prompt_version="p",
                input_digest="a",
                response_digest="b",
            )

        def compile_sop(self, analysis, rag_chunks):
            captured["analysis"] = analysis
            timeline = analysis["reference_timeline"]
            steps = [
                {
                    "index": 1,
                    "name": "取出齿轮箱壳体",
                    "tools": [],
                    "objects": ["gearbox_housing"],
                    "actions": ["extract"],
                    "observable_checks": ["组件已取出"],
                    "unobservable_checks": [],
                    "weight": 1.0,
                    "evidence_refs": [f"video@{timeline[0]['timestamp_ms']}ms"],
                },
                {
                    "index": 2,
                    "name": "拧松螺丝",
                    "tools": ["torx_screwdriver"],
                    "objects": ["screw"],
                    "actions": ["loosen"],
                    "observable_checks": ["螺丝已拧松"],
                    "unobservable_checks": [],
                    "weight": 1.0,
                    "evidence_refs": [f"video@{timeline[-1]['timestamp_ms']}ms"],
                },
            ]
            return {"steps": steps}, ProviderReceipt(
                provider="v",
                mode="live",
                model_id="m",
                prompt_version="p",
                input_digest="a",
                response_digest="b",
            )

    row = sop_service.compile_sop_from_media(
        db,
        provider=_VocabProvider(),
        process_key="impact_gearbox_disassembly_demo",
        reference_video_asset_id=video_asset,
        manual_asset_ids=[],
    )
    # 词表注入：compile_sop 收到 controlled_vocab，且包含齿轮箱对象
    assert "controlled_vocab" in captured["analysis"]
    assert "gearbox_housing" in captured["analysis"]["controlled_vocab"]["objects"]
    assert "loosen" in captured["analysis"]["controlled_vocab"]["actions"]
    # 词表内输出通过语义门禁并可发布
    assert row.status.value == "draft"
    from gongxu.sop.service import publish_sop

    pub = publish_sop(db, row.id)
    assert pub.status.value == "published"


def test_unknown_process_compile_requires_teacher_vocabulary_confirmation(db, video_asset) -> None:
    """新工序可生成词表提案，但未由教师确认前不能发布。"""
    from gongxu.providers.base import ProviderReceipt
    from gongxu.sop import service as sop_service
    from gongxu.sop.vocab import save_process_vocabulary

    class _NewProcessProvider:
        name = "new-process"
        mode = "live"

        def analyze_reference_segment(self, segment):
            return {"steps_found": 1}, ProviderReceipt(
                provider="v",
                mode="live",
                model_id="m",
                prompt_version="p",
                input_digest="a",
                response_digest="b",
            )

        def compile_sop(self, analysis, rag_chunks):
            timestamp_ms = analysis["reference_timeline"][0]["timestamp_ms"]
            return {
                "steps": [
                    {
                        "index": 1,
                        "name": "安装滤芯",
                        "tools": ["filter_wrench"],
                        "objects": ["filter_cartridge"],
                        "actions": ["install"],
                        "observable_checks": ["滤芯已就位"],
                        "unobservable_checks": [],
                        "weight": 100,
                        "evidence_refs": [f"video@{timestamp_ms}ms"],
                    }
                ]
            }, ProviderReceipt(
                provider="v",
                mode="live",
                model_id="m",
                prompt_version="p",
                input_digest="a",
                response_digest="b",
            )

    row = sop_service.compile_sop_from_media(
        db,
        provider=_NewProcessProvider(),
        process_key="filter_replacement_demo",
        reference_video_asset_id=video_asset,
        manual_asset_ids=[],
    )
    with pytest.raises(sop_service.SopPublishError, match="尚未由教师确认"):
        sop_service.publish_sop(db, row.id)

    save_process_vocabulary(
        db,
        row.process_key,
        tools=["filter_wrench"],
        objects=["filter_cartridge"],
        actions=["install"],
        approved=True,
        updated_by="teacher",
    )
    assert sop_service.publish_sop(db, row.id).status.value == "published"


def test_vocabulary_api_exposes_builtin_and_confirms_custom_vocab(db) -> None:
    _make_user(db, role=UserRole.instructor, username="vocab_teacher")
    client = _login(db, "vocab_teacher")

    builtin = client.get("/api/v1/sops/vocabularies/impact_grinder_safe_subassembly")
    assert builtin.status_code == 200
    assert builtin.json()["approved"] is True

    saved = client.patch(
        "/api/v1/sops/vocabularies/filter_replacement_demo",
        json={
            "tools": ["filter_wrench"],
            "objects": ["filter_cartridge"],
            "actions": ["install"],
            "approved": True,
        },
    )
    assert saved.status_code == 200
    assert saved.json() == {
        "process_key": "filter_replacement_demo",
        "tools": ["filter_wrench"],
        "objects": ["filter_cartridge"],
        "actions": ["install"],
        "approved": True,
        "source": "teacher_confirmed",
    }


def test_compile_rejects_out_of_vocab_objects(db, video_asset) -> None:
    """词表外对象 → 语义门禁拒绝，不落库（fail-closed，不兜底）。"""
    from gongxu.providers.base import ProviderReceipt
    from gongxu.sop import service as sop_service

    class _BadVocabProvider:
        name = "badvocab"
        mode = "live"

        def analyze_reference_segment(self, segment):
            return {"steps_found": 1}, ProviderReceipt(
                provider="v",
                mode="live",
                model_id="m",
                prompt_version="p",
                input_digest="a",
                response_digest="b",
            )

        def compile_sop(self, analysis, rag_chunks):
            timestamp_ms = analysis["reference_timeline"][0]["timestamp_ms"]
            steps = [
                {
                    "index": 1,
                    "name": "使用示波器",
                    "tools": ["vectorscope"],  # 词表外工具
                    "objects": [],
                    "actions": ["inspect"],
                    "observable_checks": ["检查"],
                    "unobservable_checks": [],
                    "weight": 1.0,
                    "evidence_refs": [f"video@{timestamp_ms}ms"],
                }
            ]
            return {"steps": steps}, ProviderReceipt(
                provider="v",
                mode="live",
                model_id="m",
                prompt_version="p",
                input_digest="a",
                response_digest="b",
            )

    import pytest

    with pytest.raises(sop_service.SopCompileError, match="语义门禁"):
        sop_service.compile_sop_from_media(
            db,
            provider=_BadVocabProvider(),
            process_key="impact_gearbox_disassembly_demo",
            reference_video_asset_id=video_asset,
            manual_asset_ids=[],
        )
    from gongxu.db.models import SopVersion

    assert db.query(SopVersion).count() == 0


def test_async_compile_job_reports_progress_and_result(
    db, test_db, video_asset, monkeypatch
) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_user(db, role=UserRole.instructor, username="async_inst")
    client = _login(db, "async_inst")
    queued = client.post(
        "/api/v1/sops/compile-jobs",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    )
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]

    from gongxu.worker import _claim_and_run

    assert _claim_and_run(test_db) is True
    progress = client.get(f"/api/v1/jobs/{job_id}")
    assert progress.status_code == 200
    body = progress.json()
    assert body["status"] == "succeeded"
    assert body["progress_percent"] == 100
    assert body["stage"] == "draft_ready"
    assert body["result"]["sop_id"]
    published = client.post(f"/api/v1/sops/{body['result']['sop_id']}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "published"


def _recovery_sop(
    db,
    *,
    process_key: str,
    video_asset_id: str,
    source_asset_ids: list[str],
    compiled_by: str,
    version: int,
):
    from gongxu.db.models import SopPublishStatus, SopVersion, SourceGrade

    row = SopVersion(
        process_key=process_key,
        version=version,
        status=SopPublishStatus.draft,
        source_grade=SourceGrade.video_reference,
        steps=[],
        scoring_profile={},
        reference_asset_id=video_asset_id,
        source_asset_ids=source_asset_ids,
        compiled_by=compiled_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_compile_job_recovery_does_not_reuse_different_manual_set(db, monkeypatch) -> None:
    from gongxu.jobs import service as jobs_service
    from gongxu.worker import _run_sop_compile_job

    process_key = "manual-sensitive-process"
    video_id = "video-source"
    compiler_id = "compiler-1"
    job = jobs_service.enqueue(
        db,
        job_type="sop_compile",
        payload={
            "process_key": process_key,
            "reference_video_asset_id": video_id,
            "manual_asset_ids": ["manual-b"],
            "requested_by": compiler_id,
        },
        idempotency_key="compile-manual-b",
    )
    wrong_draft = _recovery_sop(
        db,
        process_key=process_key,
        video_asset_id=video_id,
        source_asset_ids=[video_id, "manual-a"],
        compiled_by=compiler_id,
        version=1,
    )
    compiled: dict[str, object] = {}

    def _compile(db_session, **kwargs):
        compiled.update(kwargs)
        return _recovery_sop(
            db_session,
            process_key=process_key,
            video_asset_id=video_id,
            source_asset_ids=[video_id, "manual-b"],
            compiled_by=compiler_id,
            version=2,
        )

    monkeypatch.setattr("gongxu.providers.factory.get_provider", lambda: object())
    monkeypatch.setattr("gongxu.sop.service.compile_sop_from_media", _compile)
    monkeypatch.setattr("gongxu.worker._lease_heartbeat", lambda _job_id: nullcontext())

    _run_sop_compile_job(db, job)

    db.refresh(job)
    assert compiled["manual_asset_ids"] == ["manual-b"]
    assert job.result is not None
    assert job.result["sop_id"] != wrong_draft.id
    assert job.result["recovered"] is False


def test_compile_job_recovery_accepts_only_exact_source_set(db, monkeypatch) -> None:
    from gongxu.jobs import service as jobs_service
    from gongxu.worker import _run_sop_compile_job

    process_key = "exact-source-process"
    video_id = "video-source"
    compiler_id = "compiler-2"
    job = jobs_service.enqueue(
        db,
        job_type="sop_compile",
        payload={
            "process_key": process_key,
            "reference_video_asset_id": video_id,
            "manual_asset_ids": ["manual-a", "manual-b"],
            "requested_by": compiler_id,
        },
        idempotency_key="compile-exact-source-set",
    )
    exact_draft = _recovery_sop(
        db,
        process_key=process_key,
        video_asset_id=video_id,
        source_asset_ids=["manual-b", video_id, "manual-a"],
        compiled_by=compiler_id,
        version=1,
    )

    def _must_not_compile(*args, **kwargs):
        raise AssertionError("精确来源集合已经存在时不应再次调用 Provider")

    monkeypatch.setattr("gongxu.providers.factory.get_provider", _must_not_compile)
    monkeypatch.setattr("gongxu.sop.service.compile_sop_from_media", _must_not_compile)

    _run_sop_compile_job(db, job)

    db.refresh(job)
    assert job.result == {
        "sop_id": exact_draft.id,
        "sop_version": exact_draft.version,
        "status": "draft",
        "recovered": True,
    }


def test_teaching_job_does_not_recover_a_legacy_sop_without_teaching(db, monkeypatch) -> None:
    from gongxu.jobs import service as jobs_service
    from gongxu.worker import _run_sop_compile_job

    job = jobs_service.enqueue(
        db,
        job_type="sop_compile",
        idempotency_key="compile-teaching",
        payload={
            "process_key": "teaching-process",
            "reference_video_asset_id": "video",
            "manual_asset_ids": [],
            "requested_by": "teacher",
            "teaching_mode": True,
        },
    )
    legacy = _recovery_sop(
        db,
        process_key="teaching-process",
        video_asset_id="video",
        source_asset_ids=["video"],
        compiled_by="teacher",
        version=1,
    )
    legacy.steps = [{"index": 1, "name": "legacy step"}]
    db.commit()

    def compile_double(db_session, **kwargs):
        assert kwargs["teaching_mode"] is True
        return _recovery_sop(
            db_session,
            process_key="teaching-process",
            video_asset_id="video",
            source_asset_ids=["video"],
            compiled_by="teacher",
            version=2,
        )

    monkeypatch.setattr("gongxu.providers.factory.get_provider", lambda: object())
    monkeypatch.setattr("gongxu.sop.service.compile_sop_from_media", compile_double)
    monkeypatch.setattr("gongxu.worker._lease_heartbeat", lambda _job_id: nullcontext())
    _run_sop_compile_job(db, job)
    db.refresh(job)
    assert job.result["sop_id"] != legacy.id and job.result["recovered"] is False


def test_patch_published_sop_creates_new_draft_and_learner_sees_only_published(
    db, video_asset, monkeypatch
) -> None:
    _inject_explicit_replay(monkeypatch)
    _make_user(db, role=UserRole.instructor, username="editor")
    instructor = _login(db, "editor")
    compiled = instructor.post(
        "/api/v1/sops/compile",
        json={
            "process_key": "impact_grinder_safe_subassembly",
            "reference_video_asset_id": video_asset,
            "manual_asset_ids": [],
        },
    ).json()
    published = instructor.post(f"/api/v1/sops/{compiled['id']}/publish").json()
    edited_steps = published["steps"]
    edited_steps[0]["name"] = "确认设备已经断电"
    patched = instructor.patch(
        f"/api/v1/sops/{published['id']}",
        json={"steps": edited_steps},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["id"] != published["id"]
    assert patched.json()["status"] == "draft"
    assert patched.json()["version"] == published["version"] + 1

    _make_user(db, role=UserRole.learner, username="sop_reader")
    learner = _login(db, "sop_reader")
    listing = learner.get("/api/v1/sops")
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()] == [published["id"]]
    assert learner.get(f"/api/v1/sops/{patched.json()['id']}").status_code == 404
