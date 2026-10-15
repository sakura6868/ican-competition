"""Prepare and run Qwen development validation on official IndustReal v2 val videos.

The official test split is never read. Ground truth comes from the published
``PSR_labels_with_errors.csv`` files and is mapped deterministically to two task-
specific SOPs. This is external-provider validation, not local model training.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.assets import service as asset_service
from gongxu.config import PROJECT_ROOT
from gongxu.data.manifest import sha256_file
from gongxu.db.models import (
    SopPublishStatus,
    SopVersion,
    SourceAsset,
    SourceGrade,
    User,
    UserRole,
    utcnow,
)
from gongxu.db.session import get_session_factory, init_db
from gongxu.evaluation.qwen import preflight_manifest, run_evaluation
from gongxu.sop.schema import EvidenceRef, ScoringProfile, SopStep

_OFFICIAL_DOI = "https://doi.org/10.4121/b008dd74-020d-4ea4-a8ba-7bb60769d224.v2"
_LICENSE = "Apache-2.0"
_DATA_ROOT = PROJECT_ROOT / "data" / "raw" / "industreal-v2" / "development"
_CONTRACT_VERSION = "industreal-qwen-ground-truth-v5"


def _selection_suffix(case_ids: list[str]) -> str:
    canonical = "\n".join(sorted(set(case_ids)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    return f"-selected-{digest}"


@dataclass(frozen=True)
class CanonicalStep:
    label_id: int
    name: str
    action: str
    object_name: str


_TASK_STEPS: dict[str, tuple[CanonicalStep, ...]] = {
    "assy": (
        CanonicalStep(3, "安装前车架", "install_front_chassis", "front_chassis"),
        CanonicalStep(6, "安装前车架销", "install_front_chassis_pin", "front_chassis_pin"),
        CanonicalStep(9, "安装后车架", "install_rear_chassis", "rear_chassis"),
        CanonicalStep(
            15,
            "安装前侧后车架销",
            "install_front_rear_chassis_pin",
            "front_rear_chassis_pin",
        ),
        CanonicalStep(
            18,
            "安装后侧后车架销",
            "install_rear_rear_chassis_pin",
            "rear_rear_chassis_pin",
        ),
        CanonicalStep(21, "安装前支架", "install_front_bracket", "front_bracket"),
        CanonicalStep(
            24,
            "安装前支架螺钉",
            "install_front_bracket_screw",
            "front_bracket_screw",
        ),
        CanonicalStep(27, "安装前轮组件", "install_front_wheel_assembly", "front_wheel_assembly"),
        CanonicalStep(30, "安装后轮组件", "install_rear_wheel_assembly", "rear_wheel_assembly"),
    ),
    "main": (
        CanonicalStep(32, "拆下后轮组件", "remove_rear_wheel_assembly", "rear_wheel_assembly"),
        CanonicalStep(
            20,
            "拆下后侧后车架销",
            "remove_rear_rear_chassis_pin",
            "rear_rear_chassis_pin",
        ),
        CanonicalStep(11, "拆下后车架", "remove_rear_chassis", "rear_chassis"),
        CanonicalStep(
            17,
            "拆下前侧后车架销",
            "remove_front_rear_chassis_pin",
            "front_rear_chassis_pin",
        ),
        CanonicalStep(
            12,
            "安装短后车架",
            "install_short_rear_chassis",
            "short_rear_chassis",
        ),
        CanonicalStep(
            15,
            "安装前侧后车架销",
            "install_front_rear_chassis_pin",
            "front_rear_chassis_pin",
        ),
        CanonicalStep(
            18,
            "安装后侧后车架销",
            "install_rear_rear_chassis_pin",
            "rear_rear_chassis_pin",
        ),
        CanonicalStep(30, "重新安装后轮组件", "install_rear_wheel_assembly", "rear_wheel_assembly"),
    ),
}

# Official error labels that are alternatives/corrections to a canonical assembly step.
_ASSY_ERROR_TO_STEP = {
    4: 1,
    8: 2,
    11: 3,
    12: 3,
    13: 3,
    16: 4,
    17: 4,
    22: 6,
    25: 7,
    28: 8,
    31: 9,
}
_MAIN_ERROR_TO_STEP = {16: 6, 19: 7, 31: 8}


def _reference_timestamps(task: str) -> dict[int, int]:
    label_path = (
        _DATA_ROOT / "annotations" / "train" / f"01_{task}_0_1" / "PSR_labels_with_errors.csv"
    )
    first_timestamp_by_label: dict[int, int] = {}
    with label_path.open("r", encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) < 3:
                raise RuntimeError(f"参考视频标签行不完整: {label_path}")
            label_id = int(row[1])
            first_timestamp_by_label.setdefault(label_id, int(Path(row[0]).stem) * 100)
    timestamps: dict[int, int] = {}
    for step_index, step in enumerate(_TASK_STEPS[task], start=1):
        if step.label_id not in first_timestamp_by_label:
            raise RuntimeError(f"参考视频缺少标准步骤标签: {task}/{step.label_id}")
        timestamps[step_index] = first_timestamp_by_label[step.label_id]
    return timestamps


def _ensure_reference_asset(db: Session, *, task: str, authorizer: User) -> SourceAsset:
    video_path = _DATA_ROOT / "media" / "train" / f"01_{task}_0_1.mp4"
    checksum = sha256_file(video_path)
    frozen_sop = db.scalar(
        select(SopVersion)
        .where(
            SopVersion.process_key == f"industreal_v2_{task}_validation",
            SopVersion.prompt_version == _CONTRACT_VERSION,
        )
        .order_by(SopVersion.version.desc())
    )
    if frozen_sop is not None and frozen_sop.reference_asset_id:
        frozen_asset = db.get(SourceAsset, frozen_sop.reference_asset_id)
        if (
            frozen_asset is not None
            and frozen_asset.sha256 == checksum
            and frozen_asset.allow_provider_export
            and frozen_asset.encrypted_path
            and Path(frozen_asset.encrypted_path).is_file()
        ):
            return frozen_asset
        raise RuntimeError("既有 IndustReal 参考资产与冻结 SOP 契约不一致")
    existing = db.scalar(
        select(SourceAsset)
        .where(
            SourceAsset.kind == "video",
            SourceAsset.sha256 == checksum,
            SourceAsset.allow_provider_export.is_(True),
        )
        .order_by(SourceAsset.created_at.desc())
    )
    if existing is not None and existing.encrypted_path and Path(existing.encrypted_path).is_file():
        return existing
    return asset_service.register_asset(
        db,
        kind="video",
        filename=video_path.name,
        temp_path=video_path,
        license_note="Official IndustReal v2 Apache-2.0 standard reference video",
        authorized_by=authorizer.id,
        sha256=checksum,
        allow_provider_export=True,
    )


def _sop_steps(
    task: str,
    *,
    reference_asset_id: str,
    reference_timestamps: dict[int, int],
) -> list[SopStep]:
    milestone_predecessors = _milestone_predecessors(reference_timestamps)
    return [
        SopStep(
            id=f"industreal-{task}-{index:02d}",
            index=index,
            name=item.name,
            predecessors=milestone_predecessors[index],
            critical=True,
            tools=[],
            objects=[item.object_name],
            actions=[item.action],
            expected_duration_s=45,
            completion_checks=[f"已完成：{item.name}"],
            weight=1.0,
            evidence_refs=[
                EvidenceRef(
                    kind="video",
                    ref=f"{reference_timestamps[index]}ms",
                    description=f"IndustReal train 标准视频：{item.name}",
                    asset_id=reference_asset_id,
                    timestamp_ms=reference_timestamps[index],
                )
            ],
        )
        for index, item in enumerate(_TASK_STEPS[task], start=1)
    ]


def _milestone_predecessors(reference_timestamps: dict[int, int]) -> dict[int, list[int]]:
    """Build a DAG from official completion milestones, preserving parallel steps."""
    groups: dict[int, list[int]] = {}
    for step_index, timestamp_ms in reference_timestamps.items():
        groups.setdefault(timestamp_ms, []).append(step_index)
    predecessors: dict[int, list[int]] = {}
    previous_group: list[int] = []
    for timestamp_ms in sorted(groups):
        current_group = sorted(groups[timestamp_ms])
        for step_index in current_group:
            predecessors[step_index] = previous_group.copy()
        previous_group = current_group
    return predecessors


def ensure_validation_sop(db: Session, *, task: str, actor_id: str) -> SopVersion:
    process_key = f"industreal_v2_{task}_validation"
    authorizer = db.get(User, actor_id)
    if authorizer is None:
        raise RuntimeError("参考视频授权账号不存在")
    reference_asset = _ensure_reference_asset(db, task=task, authorizer=authorizer)
    steps = _sop_steps(
        task,
        reference_asset_id=reference_asset.id,
        reference_timestamps=_reference_timestamps(task),
    )
    expected_steps = [step.model_dump(mode="json") for step in steps]
    existing = db.scalar(
        select(SopVersion)
        .where(
            SopVersion.process_key == process_key,
            SopVersion.prompt_version == _CONTRACT_VERSION,
        )
        .order_by(SopVersion.version.desc())
    )
    if existing is not None:
        if (
            existing.steps != expected_steps
            or existing.status != SopPublishStatus.published
            or existing.reference_asset_id != reference_asset.id
        ):
            raise RuntimeError(f"既有 {process_key} SOP 与固定验证契约不一致")
        return existing

    latest = db.scalar(
        select(SopVersion)
        .where(SopVersion.process_key == process_key)
        .order_by(SopVersion.version.desc())
    )
    row = SopVersion(
        process_key=process_key,
        version=(latest.version + 1) if latest is not None else 1,
        status=SopPublishStatus.published,
        source_grade=SourceGrade.video_reference,
        steps=expected_steps,
        scoring_profile=ScoringProfile().model_dump(mode="json"),
        prompt_version=_CONTRACT_VERSION,
        reference_asset_id=reference_asset.id,
        compiled_by=actor_id,
        published_at=utcnow(),
        source_asset_ids=[reference_asset.id],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _official_labels(path: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) < 3:
                raise RuntimeError(f"官方标签行不完整: {path}")
            rows.append((int(row[1]), row[2]))
    return rows


def _truth_for_recording(
    task: str,
    labels: list[tuple[int, str]],
) -> tuple[str, list[dict[str, Any]]]:
    present = {label_id for label_id, _name in labels}
    failed_steps: set[int] = set()
    canonical = _TASK_STEPS[task]
    for step_index, step in enumerate(canonical, start=1):
        if step.label_id not in present:
            failed_steps.add(step_index)
    if task == "assy":
        for label_id, label_name in labels:
            mapped = _ASSY_ERROR_TO_STEP.get(label_id)
            if mapped is not None or label_name.lower().startswith("incorrectly"):
                if mapped is None:
                    raise RuntimeError(f"未映射的官方错误标签: {label_id} {label_name}")
                failed_steps.add(mapped)
    else:
        for label_id, label_name in labels:
            mapped = _MAIN_ERROR_TO_STEP.get(label_id)
            if mapped is not None or label_name.lower().startswith("incorrectly"):
                if mapped is None:
                    raise RuntimeError(f"未映射的官方错误标签: {label_id} {label_name}")
                failed_steps.add(mapped)

    expected_steps = [
        {
            "step_index": step_index,
            "judgement": "failed" if step_index in failed_steps else "passed",
        }
        for step_index in range(1, len(canonical) + 1)
    ]
    return ("fail" if failed_steps else "pass"), expected_steps


def prepare_manifest(
    db: Session,
    *,
    task: str,
    authorizer: User,
    output_path: Path,
    split: str = "val",
    max_cases: int | None = None,
    case_ids: list[str] | None = None,
) -> Path:
    if task not in _TASK_STEPS:
        raise ValueError(task)
    sop = ensure_validation_sop(db, task=task, actor_id=authorizer.id)
    if split not in {"train", "val"}:
        raise ValueError(f"不支持的 IndustReal split: {split}")
    media_dir = _DATA_ROOT / "media" / split
    annotation_dir = _DATA_ROOT / "annotations" / split
    videos = sorted(media_dir.glob(f"*_{task}_*.mp4"))
    if case_ids:
        selected = set(case_ids)
        videos = [video for video in videos if video.stem in selected]
    if max_cases is not None:
        videos = videos[:max_cases]
    if not videos:
        raise RuntimeError(f"没有找到 IndustReal val/{task} 视频")

    cases: list[dict[str, Any]] = []
    for video in videos:
        recording_id = video.stem
        label_path = annotation_dir / recording_id / "PSR_labels_with_errors.csv"
        if not label_path.is_file():
            raise RuntimeError(f"缺少官方真值: {label_path}")
        expected_result, expected_steps = _truth_for_recording(task, _official_labels(label_path))
        cases.append(
            {
                "case_id": recording_id,
                "video_path": os.path.relpath(video, output_path.parent),
                "sha256": sha256_file(video),
                "authorization_note": (
                    "4TU official IndustReal v2 Apache-2.0; operator explicitly "
                    "acknowledged Qwen keyframe processing"
                ),
                "expected_result": expected_result,
                "expected_steps": expected_steps,
            }
        )

    payload = {
        "schema_version": "qwen-authorized-evaluation-v1",
        "dataset_id": (
            f"industreal-v2-{split}-{task}"
            + (f"-pilot{max_cases}" if max_cases is not None else "")
            + (_selection_suffix(case_ids) if case_ids else "")
        ),
        "evaluation_role": "development_validation",
        "provider_export_authorized": True,
        "authorization_scope": ("official_public_dataset_license_plus_operator_acknowledgement"),
        "authorization_note": (
            "Official IndustReal v2 validation videos under Apache-2.0; "
            "operator explicitly authorized necessary compressed keyframes for Qwen processing"
        ),
        "official_source_url": _OFFICIAL_DOI,
        "license_id": _LICENSE,
        "operator_acknowledged_provider_processing": True,
        "authorized_by_username": authorizer.username,
        "process_key": sop.process_key,
        "sop_version_id": sop.id,
        "threshold_tuning_prohibited": True,
        "cases": cases,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def run(
    *,
    username: str,
    output_dir: Path,
    execute: bool,
    split: str = "val",
    max_cases: int | None = None,
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    source_manifest = _DATA_ROOT / "manifest.json"
    source = json.loads(source_manifest.read_text(encoding="utf-8"))
    if source.get("official_doi") != _OFFICIAL_DOI or source.get("license") != _LICENSE:
        raise RuntimeError("本地 IndustReal 来源清单与固定官方 DOI/许可不一致")
    if source.get("test_archives_accessed") is not False:
        raise RuntimeError("本流程只允许未访问官方 test 的开发数据根")

    init_db()
    db = get_session_factory()()
    try:
        authorizer = db.scalar(select(User).where(User.username == username))
        if authorizer is None or not authorizer.is_active:
            raise RuntimeError("授权操作人账号不存在或已停用")
        if authorizer.role not in (UserRole.admin, UserRole.instructor):
            raise RuntimeError("授权操作人必须是 admin 或 instructor")

        summaries: dict[str, Any] = {}
        for task in ("assy", "main"):
            task_case_ids = (
                [case_id for case_id in case_ids if f"_{task}_" in case_id] if case_ids else None
            )
            if case_ids and not task_case_ids:
                continue
            run_suffix = f"-{split}-{task}"
            if max_cases is not None:
                run_suffix += f"-pilot{max_cases}"
            if task_case_ids:
                run_suffix += _selection_suffix(task_case_ids)
            manifest_path = output_dir / f"qwen-evaluation-industreal-v2{run_suffix}.json"
            prepare_manifest(
                db,
                task=task,
                authorizer=authorizer,
                output_path=manifest_path,
                split=split,
                max_cases=max_cases,
                case_ids=task_case_ids,
            )
            preflight = preflight_manifest(db, manifest_path)
            if execute:
                report_path = (
                    PROJECT_ROOT
                    / "reports"
                    / "generated"
                    / f"qwen-evaluation-industreal-v2{run_suffix}-qwen-eval-v19.json"
                )
                report = run_evaluation(db, preflight, output_path=report_path)
                summaries[task] = report["metrics"]
            else:
                summaries[task] = {
                    "preflight": "passed",
                    "case_count": len(preflight.cases),
                    "provider_calls": 0,
                }
        return summaries
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="IndustReal v2 val Qwen 开发验证")
    parser.add_argument("--acknowledge-external-provider-processing", action="store_true")
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "manifests",
    )
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--split", choices=("train", "val"), default="val")
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="仅评测指定 recording id，可重复传入",
    )
    args = parser.parse_args()
    if not args.acknowledge_external_provider_processing:
        print("拒绝执行：缺少外部 Provider 处理明确确认", file=sys.stderr)
        raise SystemExit(2)
    try:
        summary = run(
            username=args.username,
            output_dir=args.output_dir.expanduser().resolve(),
            execute=args.run,
            split=args.split,
            max_cases=args.max_cases,
            case_ids=args.case_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"IndustReal Qwen 验证失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.run and any(
        int(metrics.get("execution_failure_count", 0)) > 0 for metrics in summary.values()
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
