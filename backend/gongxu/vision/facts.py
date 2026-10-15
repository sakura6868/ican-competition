"""观察事实层：从帧/检测结果构建观察事件流与质量门控（handoff §6/§7）。

观察事件统一包含：timestamp_ms、左右手关键点、工具/部件检测、关系边、动作、
状态、质量、帧哈希。关系边形如 right_hand--holds-->wrench、wrench--contacts-->guard_nut。

质量门控（§10）：关系置信度门槛 0.60、有效可见帧比例 ≥60%、连续证据 ≥1s；
候选步骤允许置信度 ≥0.85 的短时语义证据，但必须同时包含动作和至少一个
工具/对象关系，且会降权。候选编号本身不是语义证据。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import cast

RELATION_CONFIDENCE_THRESHOLD = 0.60
SHORT_EVIDENCE_CONFIDENCE_THRESHOLD = 0.85
SHORT_EVIDENCE_CONFIDENCE_FACTOR = 0.75
MIN_VISIBLE_FRAME_RATIO = 0.60
MIN_CONTINUOUS_EVIDENCE_S = 1.0
DEFAULT_EVENT_GAP_MS = 2500
MAX_EVENT_GAP_MS = 10_000
MAX_COMPLEMENTARY_GAP_MS = 2500
OBSERVABLE_CHECK_PASSED = "passed"
OBSERVABLE_CHECK_FAILED = "failed"
OBSERVABLE_CHECK_INSUFFICIENT = "insufficient_evidence"
OBSERVABLE_CHECK_STATUSES = {
    OBSERVABLE_CHECK_PASSED,
    OBSERVABLE_CHECK_FAILED,
}


@dataclass
class Detection:
    """单帧检测结果（由手部/工具检测器产生）。"""

    timestamp_ms: int
    hand: str = ""  # left | right
    tool: str = ""
    object: str = ""
    action: str = ""
    relation: str = ""  # holds | contacts | operates | ...
    confidence: float = 0.0
    frame_hash: str = ""
    frame_index: int = -1
    phase: str = ""
    candidate_step_index: int | None = None
    evidence_reason: str = ""
    # Reference-guided prompts intentionally suppress non-target events. Their
    # sparse output is valid step evidence, but cannot measure camera quality.
    quality_eligible: bool = True
    # A coarse candidate superseded by reference evidence remains useful for
    # visibility accounting, but must not compete in step alignment.
    alignment_eligible: bool = True
    # Only exact SOP check text with an explicit visual status belongs here.
    # An absent key means "not observed", never an implicit pass.
    observable_check_results: dict[str, str] = field(default_factory=dict)


@dataclass
class Observation:
    """过滤后的连续观察事件或降权的候选步骤短时证据。"""

    timestamp_ms: int
    hand: str
    tool: str
    object: str
    action: str
    relation: str
    confidence: float
    frame_hash: str
    start_ms: int = 0
    end_ms: int = 0
    sample_count: int = 1
    phase: str = ""
    candidate_step_index: int | None = None
    evidence_reason: str = ""
    observable_check_results: dict[str, str] = field(default_factory=dict)
    relation_key: str = field(init=False)

    def __post_init__(self) -> None:
        self.relation_key = "||".join(
            part for part in (self.hand, self.tool, self.object, self.action) if part
        )


def hash_frame(frame: bytes) -> str:
    return hashlib.sha256(frame).hexdigest()[:16]


def build_observations(detections: list[Detection]) -> list[Observation]:
    """按置信度门槛过滤，并合并相邻的同类事件。

    合并窗口根据当前采样间隔自适应，最多容忍一个短时漏检；
    间隔过长的同类动作会保留为两个独立事件，避免把全片中多次
    出现的通用动作压成一个时间点。
    """
    qualified = [
        d
        for d in detections
        if d.alignment_eligible
        and d.confidence >= RELATION_CONFIDENCE_THRESHOLD
    ]
    qualified.sort(key=lambda d: d.timestamp_ms)

    by_relation: dict[str, list[Detection]] = {}
    by_candidate_step: dict[int, list[Detection]] = {}
    for d in qualified:
        if d.candidate_step_index is not None:
            if d.tool or d.object or d.action or d.observable_check_results:
                by_candidate_step.setdefault(d.candidate_step_index, []).append(d)
            continue
        key = "||".join(part for part in (d.hand, d.tool, d.object, d.action) if part)
        if not key:
            continue
        by_relation.setdefault(key, []).append(d)

    observations: list[Observation] = []
    gap_tolerance_ms = _event_gap_tolerance_ms(qualified)
    for items in by_relation.values():
        clusters = _split_temporal_clusters(items, gap_tolerance_ms)
        for cluster in clusters:
            observation = _cluster_to_observation(cluster)
            if observation is not None:
                observations.append(observation)

    # 模型按同一候选步骤输出的相邻帧可能分别只看清工具、对象或动作。
    # 仅在三个语义字段都不冲突时合并；短时证据还必须达到更高置信度，
    # 避免把普通单帧或 hand_present 兜底当作步骤完成证据。
    complementary_gap_ms = min(gap_tolerance_ms, MAX_COMPLEMENTARY_GAP_MS)
    for items in by_candidate_step.values():
        for cluster in _split_compatible_candidate_clusters(items, complementary_gap_ms):
            observation = _cluster_to_observation(cluster, allow_short_candidate=True)
            if observation is not None:
                observations.append(observation)
    observations.sort(key=lambda o: o.timestamp_ms)
    return observations


def _split_temporal_clusters(
    items: list[Detection], gap_tolerance_ms: int
) -> list[list[Detection]]:
    clusters: list[list[Detection]] = []
    for item in items:
        if not clusters or item.timestamp_ms - clusters[-1][-1].timestamp_ms > gap_tolerance_ms:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _split_compatible_candidate_clusters(
    items: list[Detection], gap_tolerance_ms: int
) -> list[list[Detection]]:
    clusters: list[list[Detection]] = []
    for item in items:
        if (
            not clusters
            or item.timestamp_ms - clusters[-1][-1].timestamp_ms > gap_tolerance_ms
            or not _semantic_fields_compatible(clusters[-1], item)
        ):
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _semantic_fields_compatible(items: list[Detection], candidate: Detection) -> bool:
    """只合并没有工具、对象或动作冲突的候选步骤帧。"""
    for field_name in ("tool", "object", "action"):
        existing = {getattr(item, field_name) for item in items if getattr(item, field_name)}
        value = getattr(candidate, field_name)
        if value and existing and value not in existing:
            return False
    return True


def _event_gap_tolerance_ms(detections: list[Detection]) -> int:
    timestamps = sorted({d.timestamp_ms for d in detections})
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False) if b > a]
    if not gaps:
        return DEFAULT_EVENT_GAP_MS
    gaps.sort()
    median_gap = gaps[len(gaps) // 2]
    # 2.5 倍中位采样间隔可容忍约一个缺失采样点。
    return min(MAX_EVENT_GAP_MS, max(DEFAULT_EVENT_GAP_MS, round(median_gap * 2.5)))


def _cluster_to_observation(
    items: list[Detection], *, allow_short_candidate: bool = False
) -> Observation | None:
    evidence_items = items
    start_ms = items[0].timestamp_ms
    end_ms = items[-1].timestamp_ms
    is_continuous = (
        len(items) >= 2
        and (end_ms - start_ms) / 1000.0 >= MIN_CONTINUOUS_EVIDENCE_S
    )
    confidence_factor = 1.0
    if not is_continuous:
        if not allow_short_candidate or items[0].candidate_step_index is None:
            return None
        evidence_items = [
            item
            for item in items
            if item.confidence >= SHORT_EVIDENCE_CONFIDENCE_THRESHOLD
        ]
        if not evidence_items or not _has_short_action_relation(evidence_items):
            return None
        confidence_factor = SHORT_EVIDENCE_CONFIDENCE_FACTOR
        start_ms = evidence_items[0].timestamp_ms
        end_ms = evidence_items[-1].timestamp_ms

    rep = max(evidence_items, key=lambda detection: detection.confidence)
    reasons = [item.evidence_reason for item in evidence_items if item.evidence_reason]
    if not is_continuous:
        reasons.append("候选步骤的短时高置信视觉证据，已降权")
    return Observation(
        timestamp_ms=rep.timestamp_ms,
        hand=_merged_value(evidence_items, "hand", fallback=rep.hand),
        tool=_merged_value(evidence_items, "tool"),
        object=_merged_value(evidence_items, "object"),
        action=_merged_value(evidence_items, "action"),
        relation=rep.relation,
        confidence=round(
            sum(item.confidence for item in evidence_items)
            / len(evidence_items)
            * confidence_factor,
            4,
        ),
        frame_hash=rep.frame_hash,
        start_ms=start_ms,
        end_ms=end_ms,
        sample_count=len(evidence_items),
        phase=rep.phase,
        candidate_step_index=rep.candidate_step_index,
        evidence_reason="；".join(dict.fromkeys(reasons)),
        observable_check_results=_merged_observable_check_results(evidence_items),
    )


def _has_short_action_relation(items: list[Detection]) -> bool:
    """短时证据至少要表达“动作 + 工具/对象”，不能只有候选或动作标签。"""
    # A controlled, explicit check status is itself a visual fact. It may be
    # reported without a tool/action tuple (for example, missing PPE), but only
    # when routed to a concrete SOP step and above the short-evidence threshold.
    if any(_valid_observable_check_results(item) for item in items):
        return True
    has_action = any(item.action for item in items)
    has_tool_or_object = any(item.tool or item.object for item in items)
    has_semantic_relation = any(
        item.relation and item.relation != "hand_present" for item in items
    ) or (has_action and has_tool_or_object)
    return has_action and has_tool_or_object and has_semantic_relation


def _valid_observable_check_results(detection: Detection) -> dict[str, str]:
    return {
        check: status
        for check, status in detection.observable_check_results.items()
        if check and status in OBSERVABLE_CHECK_STATUSES
    }


def _merged_observable_check_results(items: list[Detection]) -> dict[str, str]:
    """Merge explicit check facts conservatively; a visible violation wins."""
    merged: dict[str, str] = {}
    for item in items:
        for check, status in _valid_observable_check_results(item).items():
            previous = merged.get(check)
            if previous == OBSERVABLE_CHECK_FAILED:
                continue
            if status == OBSERVABLE_CHECK_FAILED or previous is None:
                merged[check] = status
    return merged


def _merged_value(
    items: list[Detection], field_name: str, *, fallback: str = ""
) -> str:
    values = {getattr(item, field_name) for item in items if getattr(item, field_name)}
    if len(values) == 1:
        return cast(str, values.pop())
    return fallback if len(values) == 0 else ""


def visible_frame_ratio(detections: list[Detection], total_frames: int) -> float:
    """有效可见帧比例：有合格检测的帧 / 总帧。"""
    if total_frames <= 0:
        return 0.0
    qualified_frames = {
        (d.timestamp_ms, d.frame_hash)
        for d in detections
        if d.quality_eligible
        and d.confidence >= RELATION_CONFIDENCE_THRESHOLD
    }
    return round(len(qualified_frames) / total_frames, 4)


def quality_gate(detections: list[Detection], total_frames: int) -> tuple[bool, str]:
    """质量门控：可见比例 <60% 或有效观察为空时返回 (False, 拒判原因)。"""
    ratio = visible_frame_ratio(detections, total_frames)
    if ratio < MIN_VISIBLE_FRAME_RATIO:
        return False, f"有效可见帧比例 {ratio:.0%} 低于 60%，视角质量不足，无法判分"
    observations = build_observations(detections)
    if not any(obs.tool or obs.object or obs.action for obs in observations):
        return False, "未形成连续或高置信短时语义证据，无法判分"
    return True, ""
