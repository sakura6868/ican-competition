"""预录视频考评流水线（整改报告 P0-B）。

原则：无真实视频、无模型 → 结构化拒判，不生成伪精确分数。
- 正常模式必须绑定 input_asset_id，且资产存在、类型为 video。
- 模型缺失时不调用 fixture_detections，返回
  { status=inconclusive, reason_code=model_unavailable, missing_models=[...] }。
- Fixture 仅在 GONGXU_DEMO_FIXTURES=true 且显式 demo 模式启用，来源标记 fixture。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from gongxu.assets import service as asset_service
from gongxu.config import get_settings
from gongxu.db.models import AssessmentSession, Observation, SopVersion, SourceAsset
from gongxu.media.video import (
    MediaError,
    SampledFrame,
    extract_frame_at,
    extract_frames_at,
    extract_keyframes_uniform,
)
from gongxu.scoring.engine import score_assessment
from gongxu.sop.schema import SopStep
from gongxu.vision import align, facts
from gongxu.vision.adapters import (
    AdapterTransientError,
    AdapterUnavailableError,
    NoVisionAdapter,
    VisionAdapter,
    resolve_adapter,
)
from gongxu.vision.facts import Detection
from gongxu.vision.models import ensure_required_registered, list_models

# 第一遍保持全片均匀 24 帧；第二遍在候选切换附近补帧；第三遍只为
# 尚无语义证据的 SOP 步骤按预计时间槽补帧。总量和 Provider 调用仍有硬上限。
_FIRST_PASS_KEYFRAMES = 24
_MAX_TRANSITION_KEYFRAMES = 24
_MAX_TOTAL_KEYFRAMES = 96
_MAX_ADAPTIVE_TRANSITIONS = 12
_ADAPTIVE_FRACTION_ROUNDS = ((0.25, 0.75), (0.375, 0.625))
_COVERAGE_FRACTIONS = (0.2, 0.4, 0.6, 0.8)
_SAMPLING_CONTRACT_VERSION = "sampling-v13"
_MODEL_VERSION_MAX_LENGTH = 64
InferenceProgressCallback = Callable[[int, int], None]
TelemetryCallback = Callable[[dict[str, int | str]], None]
FrameSignature = tuple[
    frozenset[tuple[int | None, str]],
    frozenset[tuple[str, str, str]],
]

logger = logging.getLogger(__name__)


class VisionPipelineError(Exception):
    pass


class RejectError(Exception):
    """明确拒判：status 恒为 inconclusive，携带 reason_code 与缺失模型。"""

    def __init__(
        self,
        reason: str,
        reason_code: str = "quality_gate",
        missing_models: list[str] | None = None,
        provider_metrics: dict[str, int | str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = "inconclusive"
        self.reason_code = reason_code
        self.missing_models = missing_models or []
        self.reason = reason
        self.provider_metrics = provider_metrics or {}


def run_recorded_pipeline(
    db: Session,
    session: AssessmentSession,
    *,
    work_dir: Path,
    demo: bool = False,
    inference_progress: InferenceProgressCallback | None = None,
) -> dict[str, Any]:
    """执行预录视频 → 评分。返回评分摘要与步骤判定（供落库/报告）。

    demo=False（默认）：真实模式，模型缺失即拒判。
    demo=True：仅当 GONGXU_DEMO_FIXTURES=true 时使用 Fixture（来源标记 fixture）。
    """
    settings = get_settings()
    ensure_required_registered(db)
    provider_metrics: dict[str, int | str] = {}

    if demo and settings.demo_fixtures:
        total_frames = 24
        from gongxu.vision.fixture import fixture_detections

        detections = fixture_detections(total_keyframes=total_frames, scenario="pass")
        evidence_source = "fixture"
        model_version = "demo-fixture"
    else:
        if not session.input_asset_id:
            raise RejectError("考评会话未绑定预录视频资产", reason_code="missing_input_asset")
        asset = db.get(SourceAsset, session.input_asset_id)
        if asset is None:
            raise RejectError("预录视频资产不存在", reason_code="missing_asset")
        if asset.kind != "video":
            raise RejectError("会话绑定资产不是视频", reason_code="wrong_asset_type")
        if settings.vision_backend == "qwen" and not asset.allow_provider_export:
            raise RejectError(
                "该考评视频未授权发送至外部多模态模型",
                reason_code="provider_export_not_authorized",
            )
        missing = _missing_models(db)
        if missing:
            raise RejectError(
                f"模型缺失: {', '.join(missing)}",
                reason_code="model_unavailable",
                missing_models=missing,
            )
        from gongxu.assessment import service as assessment_service

        sop = assessment_service._load_sop(db, session.sop_version_id, session.process_key)
        detections, total_frames = _infer_detections(
            db,
            session,
            work_dir,
            sop_steps=sop.steps,
            inference_progress=inference_progress,
            telemetry_callback=provider_metrics.update,
        )
        evidence_source = "vision"
        model_version = _model_version(db, process_key=session.process_key, sop_steps=sop.steps)

    ok, reason = facts.quality_gate(detections, total_frames)
    if not ok:
        raise RejectError(
            reason,
            reason_code="quality_gate",
            provider_metrics=provider_metrics,
        )

    observations = facts.build_observations(detections)
    for obs in observations:
        db.add(
            Observation(
                session_id=session.id,
                timestamp_ms=obs.timestamp_ms,
                hand=obs.hand,
                tool=obs.tool,
                object=obs.object,
                action=obs.action,
                relation=obs.relation,
                confidence=obs.confidence,
                frame_hash=obs.frame_hash,
                payload={
                    "observable_check_results": dict(obs.observable_check_results),
                },
            )
        )
    db.commit()

    if demo and settings.demo_fixtures:
        from gongxu.assessment import service as assessment_service

        sop = assessment_service._load_sop(db, session.sop_version_id, session.process_key)
    alignment = align.align_observations(sop.steps, observations)
    result = score_assessment(sop.steps, alignment.step_results, sop.scoring_profile)
    return {
        "judgements": [
            {
                "step_index": r.step.index,
                "judgement": r.judgement,
                "start_ms": r.start_ms,
                "end_ms": r.end_ms,
                "rule_code": r.rule_code,
                "reason": r.reason,
                "confidence": r.confidence,
                "keyframe_ref": r.keyframe_ref,
                "observable_check_results": r.observable_check_results,
            }
            for r in alignment.step_results
        ],
        "observations": len(observations),
        "quality": {
            "ok": True,
            "visible_ratio": facts.visible_frame_ratio(detections, total_frames),
        },
        "scoring": {
            "total_score": result.total_score,
            "result": result.result,
            "dimension_scores": result.dimension_scores,
            "evidence_coverage": result.evidence_coverage,
        },
        "evidence_source": evidence_source,
        "model_version": model_version,
        "provider_metrics": provider_metrics,
    }


def _missing_models(db: Session) -> list[str]:
    """列出登记为 missing/experimental 的关键模型；按后端限定依赖。

    - qwen：不依赖本地模型登记。
    - onnx：需要 hand + tool_detector（tcn 为实验性动作序列模型，缺失不阻断）。
    - 其他（none）：完整门禁 hand/tool_detector/tcn。
    """
    backend = get_settings().vision_backend
    if backend == "qwen":
        return []
    required = ("hand", "tool_detector") if backend == "onnx" else ("hand", "tool_detector", "tcn")
    missing = [
        m.name for m in list_models(db) if m.name in required and m.status.value != "available"
    ]
    return sorted(missing)


def _model_version(
    db: Session,
    *,
    process_key: str | None = None,
    sop_steps: list[SopStep] | None = None,
) -> str:
    """模型版本来自实际推理适配器（onnx/qwen），无适配器时明确标记。"""
    adapter = resolve_adapter(db, process_key=process_key, sop_steps=sop_steps)
    if isinstance(adapter, NoVisionAdapter):
        return "no-vision-adapter"
    adapter_version = adapter.model_version()
    sequence_mode = "sequence" if get_settings().qwen_sequence_input_enabled else "static"
    sampling_contract = f"{_SAMPLING_CONTRACT_VERSION}-{sequence_mode}"
    version = f"{adapter_version}:{sampling_contract}"
    if len(version) <= _MODEL_VERSION_MAX_LENGTH:
        return version
    digest = hashlib.sha256(adapter_version.encode("utf-8")).hexdigest()[:12]
    return f"vision@sha256-{digest}:{sampling_contract}"


def _infer_detections(
    db: Session,
    session: AssessmentSession,
    work_dir: Path,
    *,
    sop_steps: list[SopStep] | None = None,
    inference_progress: InferenceProgressCallback | None = None,
    telemetry_callback: TelemetryCallback | None = None,
) -> tuple[list[Detection], int]:
    """真实模型推理：遍历关键帧调用统一视觉适配器；模型缺失/推理失败结构化拒判。"""
    if not session.input_asset_id:
        raise VisionPipelineError("考评会话缺少预录视频资产")
    asset = db.get(SourceAsset, session.input_asset_id)
    if asset is None:
        raise VisionPipelineError(f"预录视频资产不存在: {session.input_asset_id}")
    tmp = work_dir / "plain"
    tmp.mkdir(parents=True, exist_ok=True)
    video_path = asset_service.decrypt_to_temp(db, asset.id, temp_dir=tmp)
    try:
        frames_dir = work_dir / "keyframes"
        extracted = extract_keyframes_uniform(
            video_path,
            frames_dir / "pass1",
            count=_FIRST_PASS_KEYFRAMES,
        )
    except MediaError as exc:
        raise VisionPipelineError(f"视频抽帧失败: {exc}") from exc
    adapter = resolve_adapter(db, process_key=session.process_key, sop_steps=sop_steps)
    if isinstance(adapter, NoVisionAdapter):
        # 无真实适配器 → 不产生伪检测（P0-B.6 fail-closed）
        raise RejectError(adapter.reason, reason_code="adapter_unimplemented")
    configure_steps = getattr(adapter, "set_sop_steps", None)
    if sop_steps is not None and callable(configure_steps):
        configure_steps(sop_steps)
    _configure_reference_frames(
        db,
        session,
        adapter,
        sop_steps or [],
        work_dir=work_dir,
    )

    first_frames = _deduplicate_sampled_frames(
        _normalize_sampled_frames(extracted, frames_dir / "pass1")
    )
    if not first_frames:
        raise RejectError("视频未抽取到可用帧", reason_code="no_sampled_frames")
    first_progress: list[int] = []
    try:
        first_detections = _infer_frame_set(
            adapter,
            first_frames,
            progress=lambda processed, _total: first_progress.append(processed),
        )
    except AdapterTransientError as exc:
        raise VisionPipelineError(str(exc)) from exc
    except AdapterUnavailableError as exc:
        raise RejectError(str(exc), reason_code="adapter_unavailable") from exc

    adaptive_frames: list[SampledFrame] = []
    adaptive_detections: list[Detection] = []
    adaptive_progress: list[int] = []
    adaptive_timestamps = _adaptive_sample_timestamps(first_frames, first_detections)
    if adaptive_timestamps:
        try:
            adaptive_frames = extract_frames_at(
                video_path,
                frames_dir / "pass2",
                timestamps_ms=adaptive_timestamps,
                frame_index_start=len(first_frames),
            )
            adaptive_frames = _deduplicate_sampled_frames(adaptive_frames)
            adaptive_detections = _infer_frame_set(
                adapter,
                adaptive_frames,
                progress=lambda processed, _total: adaptive_progress.append(processed),
            )
        except Exception as exc:  # noqa: BLE001 - 第二遍是可选增强，任何失败都必须降级
            logger.warning("adaptive second-pass sampling failed; retaining first pass: %s", exc)
            adaptive_frames = []
            adaptive_detections = []
            adaptive_progress = []

    transition_frames = _deduplicate_sampled_frames([*first_frames, *adaptive_frames])
    transition_detections = [*first_detections, *adaptive_detections]
    coverage_frames: list[SampledFrame] = []
    coverage_detections: list[Detection] = []
    coverage_progress: list[int] = []
    reference_indices = getattr(adapter, "reference_step_indices", None)
    infer_step_candidates = getattr(adapter, "infer_step_candidates", None)
    available_reference_steps = set(reference_indices()) if callable(reference_indices) else set()
    coverage_plan = _coverage_sample_plan(
        first_frames,
        transition_detections,
        sop_steps or [],
        existing_timestamps={frame.timestamp_ms for frame in transition_frames},
        capacity=max(0, _MAX_TOTAL_KEYFRAMES - len(transition_frames)),
        eligible_step_indices=available_reference_steps,
        include_observed_steps=True,
    )
    if coverage_plan and callable(infer_step_candidates):
        try:
            coverage_frames = extract_frames_at(
                video_path,
                frames_dir / "pass3",
                timestamps_ms=sorted({timestamp_ms for _step_index, timestamp_ms in coverage_plan}),
                frame_index_start=len(transition_frames),
            )
            coverage_frames = _deduplicate_sampled_frames(coverage_frames)
            frames_by_step = _coverage_frames_by_step(coverage_plan, coverage_frames)
            processed = 0
            for step_index, candidate_frames in sorted(frames_by_step.items()):
                items = [
                    (frame.path.read_bytes(), frame.timestamp_ms) for frame in candidate_frames
                ]
                try:
                    per_frame = infer_step_candidates(step_index, items)
                except (AdapterTransientError, AdapterUnavailableError) as exc:
                    logger.warning(
                        "reference comparison failed for step %s: %s",
                        step_index,
                        exc,
                    )
                    processed += len(candidate_frames)
                    coverage_progress.append(processed)
                    continue
                for frame, frame_detections in zip(candidate_frames, per_frame, strict=False):
                    for detection in frame_detections:
                        detection.frame_index = frame.frame_index
                        detection.quality_eligible = False
                    coverage_detections.extend(frame_detections)
                processed += len(candidate_frames)
                coverage_progress.append(processed)
        except Exception as exc:  # noqa: BLE001 - 第三遍同样只能安全降级
            logger.warning("reference-guided third pass failed; retaining prior passes: %s", exc)
            coverage_frames = []
            coverage_detections = []
            coverage_progress = []

    all_frames = _deduplicate_sampled_frames([*transition_frames, *coverage_frames])
    index_by_timestamp = {
        frame.timestamp_ms: frame_index for frame_index, frame in enumerate(all_frames)
    }
    reference_confirmed_steps = {
        detection.candidate_step_index
        for detection in coverage_detections
        if detection.candidate_step_index is not None
        and (
            detection.tool
            or detection.object
            or detection.action
            or detection.observable_check_results
        )
    }
    if reference_confirmed_steps:
        for detection in transition_detections:
            if detection.candidate_step_index in reference_confirmed_steps:
                detection.alignment_eligible = False
    detections = [*transition_detections, *coverage_detections]
    for detection in detections:
        detection.frame_index = index_by_timestamp[detection.timestamp_ms]
    detections.sort(key=lambda detection: (detection.timestamp_ms, detection.frame_index))

    if inference_progress is not None:
        total_frames = len(all_frames)
        for processed in first_progress:
            inference_progress(processed, total_frames)
        for processed in adaptive_progress:
            inference_progress(len(first_frames) + processed, total_frames)
        for processed in coverage_progress:
            inference_progress(len(transition_frames) + processed, total_frames)
    telemetry = getattr(adapter, "telemetry", None)
    if telemetry_callback is not None and callable(telemetry):
        telemetry_callback(telemetry())
    # Only the general first/transition passes are suitable for camera-quality
    # accounting. Reference prompts deliberately return sparse target evidence.
    return detections, len(transition_frames)


def _coverage_frames_by_step(
    coverage_plan: list[tuple[int, int]],
    coverage_frames: list[SampledFrame],
) -> dict[int, list[SampledFrame]]:
    """Map shared timestamps to every target step instead of overwriting one."""
    steps_by_timestamp: dict[int, list[int]] = {}
    for step_index, timestamp_ms in coverage_plan:
        steps_by_timestamp.setdefault(timestamp_ms, []).append(step_index)
    frames_by_step: dict[int, list[SampledFrame]] = {}
    for frame in coverage_frames:
        for step_index in steps_by_timestamp.get(frame.timestamp_ms, []):
            frames_by_step.setdefault(step_index, []).append(frame)
    return frames_by_step


def _configure_reference_frames(
    db: Session,
    session: AssessmentSession,
    adapter: VisionAdapter,
    sop_steps: list[SopStep],
    *,
    work_dir: Path,
) -> None:
    """Extract standard-video step frames into the job's ephemeral directory."""
    configure = getattr(adapter, "set_reference_frames", None)
    if not callable(configure) or not session.sop_version_id:
        return
    sop_row = db.get(SopVersion, session.sop_version_id)
    if sop_row is None or not sop_row.reference_asset_id:
        return
    reference_asset = db.get(SourceAsset, sop_row.reference_asset_id)
    if reference_asset is None or not reference_asset.allow_provider_export:
        logger.warning("reference video is missing provider-export authorization")
        return
    try:
        reference_plain = asset_service.decrypt_to_temp(
            db,
            reference_asset.id,
            temp_dir=work_dir / "reference-plain",
        )
        frames: dict[int, list[bytes]] = {}
        for step in sop_steps:
            evidence = next(
                (
                    ref
                    for ref in step.evidence_refs
                    if ref.kind == "video"
                    and ref.timestamp_ms is not None
                    and (ref.asset_id or sop_row.reference_asset_id) == reference_asset.id
                ),
                None,
            )
            if evidence is None or evidence.timestamp_ms is None:
                continue
            context: list[bytes] = []
            context_timestamps = sorted(
                {
                    max(0, evidence.timestamp_ms - 4_000),
                    evidence.timestamp_ms,
                    evidence.timestamp_ms + 2_000,
                }
            )
            for position, timestamp_ms in enumerate(context_timestamps):
                output = work_dir / "reference-frames" / f"step-{step.index:03d}-{position}.jpg"
                try:
                    extract_frame_at(
                        reference_plain,
                        output,
                        timestamp_ms=timestamp_ms,
                    )
                except MediaError as exc:
                    logger.warning(
                        "reference context frame failed for step %s at %sms: %s",
                        step.index,
                        timestamp_ms,
                        exc,
                    )
                    continue
                context.append(output.read_bytes())
            if context:
                frames[step.index] = context
        configure(frames)
    except (OSError, MediaError, asset_service.AssetError) as exc:
        logger.warning("reference-frame setup failed; continuing without it: %s", exc)


def _infer_frame_set(
    adapter: VisionAdapter,
    frames: list[SampledFrame],
    *,
    progress: InferenceProgressCallback | None = None,
) -> list[Detection]:
    """Infer one sampling pass while preserving every frame's global timestamp."""
    detections: list[Detection] = []
    infer_sequence = getattr(adapter, "infer_sequence", None)
    if get_settings().qwen_sequence_input_enabled and callable(infer_sequence) and len(frames) > 1:
        sequence_size = 16
        batches = [frames[i : i + sequence_size] for i in range(0, len(frames), sequence_size)]
        processed_frames = 0
        for batch in batches:
            items = [(frame.path.read_bytes(), frame.timestamp_ms) for frame in batch]
            per_frame = infer_sequence(items)
            for frame, frame_detections in zip(batch, per_frame, strict=False):
                for detection in frame_detections:
                    detection.frame_index = frame.frame_index
                detections.extend(frame_detections)
            processed_frames += len(batch)
            if progress is not None:
                progress(processed_frames, len(frames))
        return detections

    infer_batch = getattr(adapter, "infer_batch", None)
    if callable(infer_batch) and len(frames) > 1:
        batches = [frames[i : i + 4] for i in range(0, len(frames), 4)]
        processed_frames = 0
        for batch in batches:
            items = [(frame.path.read_bytes(), frame.timestamp_ms) for frame in batch]
            per_frame = infer_batch(items)
            for frame, frame_detections in zip(batch, per_frame, strict=False):
                for detection in frame_detections:
                    detection.frame_index = frame.frame_index
                detections.extend(frame_detections)
            processed_frames += len(batch)
            if progress is not None:
                progress(processed_frames, len(frames))
        return detections

    for processed_frames, frame in enumerate(frames, start=1):
        frame_detections = adapter.infer_frame(
            frame.path.read_bytes(),
            timestamp_ms=frame.timestamp_ms,
        )
        for detection in frame_detections:
            detection.frame_index = frame.frame_index
        detections.extend(frame_detections)
        if progress is not None:
            progress(processed_frames, len(frames))
    return detections


def _adaptive_sample_timestamps(
    first_frames: list[SampledFrame], first_detections: list[Detection]
) -> list[int]:
    """Choose exact-time frames around semantic/candidate transitions.

    Each selected transition receives one frame on either side first, then a
    second pair when capacity permits.  Transitions are thinned uniformly when
    the first pass is very noisy, ensuring the combined hard limit of 48.
    """
    if len(first_frames) < 2:
        return []
    detections_by_timestamp: dict[int, list[Detection]] = {}
    for detection in first_detections:
        detections_by_timestamp.setdefault(detection.timestamp_ms, []).append(detection)

    transitions: list[tuple[int, int]] = []
    for previous, current in zip(first_frames, first_frames[1:], strict=False):
        previous_signature = _frame_signature(
            detections_by_timestamp.get(previous.timestamp_ms, [])
        )
        current_signature = _frame_signature(detections_by_timestamp.get(current.timestamp_ms, []))
        if (
            previous.timestamp_ms < current.timestamp_ms
            and previous_signature != current_signature
            and (any(previous_signature) or any(current_signature))
        ):
            transitions.append((previous.timestamp_ms, current.timestamp_ms))

    selected = _uniformly_limit_transitions(transitions, _MAX_ADAPTIVE_TRANSITIONS)
    existing = {frame.timestamp_ms for frame in first_frames}
    capacity = min(
        _MAX_TRANSITION_KEYFRAMES,
        max(0, _MAX_TOTAL_KEYFRAMES - len(existing)),
    )
    timestamps: list[int] = []
    seen = set(existing)
    for fractions in _ADAPTIVE_FRACTION_ROUNDS:
        for start_ms, end_ms in selected:
            for fraction in fractions:
                timestamp_ms = round(start_ms + (end_ms - start_ms) * fraction)
                if timestamp_ms in seen or not start_ms < timestamp_ms < end_ms:
                    continue
                timestamps.append(timestamp_ms)
                seen.add(timestamp_ms)
                if len(timestamps) >= capacity:
                    return sorted(timestamps)
    return sorted(timestamps)


def _coverage_sample_timestamps(
    first_frames: list[SampledFrame],
    detections: list[Detection],
    sop_steps: list[SopStep],
    *,
    existing_timestamps: set[int],
    capacity: int,
) -> list[int]:
    return [
        timestamp_ms
        for _step_index, timestamp_ms in _coverage_sample_plan(
            first_frames,
            detections,
            sop_steps,
            existing_timestamps=existing_timestamps,
            capacity=capacity,
            eligible_step_indices={step.index for step in sop_steps},
            include_observed_steps=False,
        )
    ]


def _coverage_sample_plan(
    first_frames: list[SampledFrame],
    detections: list[Detection],
    sop_steps: list[SopStep],
    *,
    existing_timestamps: set[int],
    capacity: int,
    eligible_step_indices: set[int],
    include_observed_steps: bool,
) -> list[tuple[int, int]]:
    """Sample estimated time slots only for required steps with no semantic evidence.

    The slot layout is derived solely from the published SOP's expected durations;
    no dataset truth or predicted result is consulted. Fractions are allocated in
    rounds so every missing step receives broad coverage before any receives a
    denser sample.
    """
    if len(first_frames) < 2 or capacity <= 0:
        return []
    required_steps = sorted(
        (step for step in sop_steps if not step.optional),
        key=lambda step: step.index,
    )
    if not required_steps:
        return []
    provisional_observations = facts.build_observations(detections)
    provisional_alignment = align.align_observations(sop_steps, provisional_observations)
    observed_steps = {
        result.step.index
        for result in provisional_alignment.step_results
        if result.judgement not in {"insufficient_evidence", "unobservable"}
    }
    missing_steps = [
        step
        for step in required_steps
        if (include_observed_steps or step.index not in observed_steps)
        and step.index in eligible_step_indices
    ]
    if not missing_steps:
        return []

    timeline_start = first_frames[0].timestamp_ms
    timeline_end = first_frames[-1].timestamp_ms
    duration_ms = timeline_end - timeline_start
    if duration_ms <= 1:
        return []
    slots = _reference_timeline_slots(
        required_steps,
        timeline_start=timeline_start,
        timeline_end=timeline_end,
    )
    slots.update(
        _observed_step_slots(
            detections,
            required_steps,
            timeline_start=timeline_start,
            timeline_end=timeline_end,
        )
    )

    plan: list[tuple[int, int]] = []
    selected_timestamps: set[int] = set()
    for fraction in _COVERAGE_FRACTIONS:
        for step in missing_steps:
            slot_start, slot_end = slots[step.index]
            timestamp_ms = round(slot_start + (slot_end - slot_start) * fraction)
            if (
                timestamp_ms in existing_timestamps
                or timestamp_ms <= timeline_start
                or timestamp_ms >= timeline_end
            ):
                continue
            if timestamp_ms not in selected_timestamps:
                if len(selected_timestamps) >= capacity:
                    continue
                selected_timestamps.add(timestamp_ms)
            pair = (step.index, timestamp_ms)
            if pair in plan:
                continue
            plan.append(pair)
    return sorted(plan, key=lambda item: (item[1], item[0]))


def _observed_step_slots(
    detections: list[Detection],
    steps: list[SopStep],
    *,
    timeline_start: int,
    timeline_end: int,
) -> dict[int, tuple[int, int]]:
    """Center reference checks on observed actions before relative-time fallback."""
    allowed = {step.index for step in steps}
    timestamps_by_step: dict[int, list[int]] = {}
    for detection in detections:
        step_index = detection.candidate_step_index
        if (
            step_index in allowed
            and detection.confidence >= facts.RELATION_CONFIDENCE_THRESHOLD
            and (detection.tool or detection.object or detection.action)
        ):
            timestamps_by_step.setdefault(step_index, []).append(detection.timestamp_ms)
    if timeline_end <= timeline_start or not timestamps_by_step:
        return {}
    half_width = max(
        4_000,
        min(12_000, round((timeline_end - timeline_start) / max(len(steps), 1) * 0.6)),
    )
    slots: dict[int, tuple[int, int]] = {}
    for step_index, timestamps in timestamps_by_step.items():
        ordered = sorted(timestamps)
        center = ordered[len(ordered) // 2]
        slot_start = max(timeline_start, center - half_width)
        slot_end = min(timeline_end, center + half_width)
        if slot_end > slot_start:
            slots[step_index] = (slot_start, slot_end)
    return slots


def _reference_timeline_slots(
    steps: list[SopStep],
    *,
    timeline_start: int,
    timeline_end: int,
) -> dict[int, tuple[int, int]]:
    """Map standard-video step timing to a target video's relative timeline."""
    duration_ms = timeline_end - timeline_start
    reference_times: dict[int, int] = {}
    for step in steps:
        timestamp = next(
            (
                ref.timestamp_ms
                for ref in step.evidence_refs
                if ref.kind == "video" and ref.timestamp_ms is not None
            ),
            None,
        )
        if timestamp is not None:
            reference_times[step.index] = timestamp

    if len(reference_times) == len(steps) and max(reference_times.values(), default=0) > 0:
        reference_end = max(reference_times.values())
        centers = {
            step.index: timeline_start
            + round(duration_ms * reference_times[step.index] / reference_end)
            for step in steps
        }
        distinct_centers = sorted(set(centers.values()))
        slots: dict[int, tuple[int, int]] = {}
        for step in steps:
            center = centers[step.index]
            prior = [value for value in distinct_centers if value < center]
            following = [value for value in distinct_centers if value > center]
            slot_start = round((prior[-1] + center) / 2) if prior else timeline_start
            slot_end = round((center + following[0]) / 2) if following else timeline_end
            slots[step.index] = (slot_start, slot_end)
        return slots

    weights = [max(float(step.expected_duration_s), 1.0) for step in steps]
    total_weight = sum(weights)
    cursor = 0.0
    slots = {}
    for step, weight in zip(steps, weights, strict=True):
        slot_start = timeline_start + round(duration_ms * cursor / total_weight)
        cursor += weight
        slot_end = timeline_start + round(duration_ms * cursor / total_weight)
        slots[step.index] = (slot_start, slot_end)
    return slots


def _frame_signature(detections: list[Detection]) -> FrameSignature:
    candidate_phase = frozenset(
        (detection.candidate_step_index, detection.phase)
        for detection in detections
        if detection.candidate_step_index is not None or detection.phase
    )
    semantics = frozenset(
        (detection.tool, detection.object, detection.action)
        for detection in detections
        if detection.tool or detection.object or detection.action
    )
    return candidate_phase, semantics


def _uniformly_limit_transitions(
    transitions: list[tuple[int, int]], limit: int
) -> list[tuple[int, int]]:
    if limit <= 0:
        return []
    if len(transitions) <= limit:
        return transitions
    return [transitions[index * len(transitions) // limit] for index in range(limit)]


def _deduplicate_sampled_frames(frames: list[SampledFrame]) -> list[SampledFrame]:
    """Keep one frame per timestamp and return a globally ordered timeline."""
    by_timestamp: dict[int, SampledFrame] = {}
    for frame in sorted(frames, key=lambda item: (item.timestamp_ms, item.frame_index)):
        by_timestamp.setdefault(frame.timestamp_ms, frame)
    return list(by_timestamp.values())


def _normalize_sampled_frames(
    extracted: list[SampledFrame] | None, frames_dir: Path
) -> list[SampledFrame]:
    """兼容旧的测试/插件抽帧器，但只在其未返回元数据时回退。"""
    if extracted:
        return sorted(extracted, key=lambda frame: (frame.timestamp_ms, frame.frame_index))
    paths = sorted(frames_dir.glob("*.jpg"))
    return [
        SampledFrame(path=path, frame_index=i, timestamp_ms=i * 1000)
        for i, path in enumerate(paths)
    ]
