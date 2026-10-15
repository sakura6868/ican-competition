"""Pure progression rules. A model never selects a successor or changes progress."""

from copy import deepcopy
from typing import Any

from gongxu.sop.schema import SopStep


def initial_state(steps: list[SopStep], now_ms: int) -> dict[str, Any]:
    first = next((s for s in sorted(steps, key=lambda s: s.index) if not s.predecessors), None)
    if first is None:
        raise ValueError("课程没有可执行的起始步骤")
    return {
        "current_step_index": first.index,
        "step_revision": 1,
        "step_state": "observing",
        "entered_at_ms": now_ms,
        "step_active_ms": 0,
        "completed": [],
        "skipped": [],
        "checks": [],
        "confirm_count": 0,
        "confirm_hashes": [],
        "confirm_sequence": 0,
        "last_sequence": 0,
        "pending_observation_id": None,
        "call_count": 0,
        "error_count": 0,
        "unknown_streak": 0,
        "step_errors": {},
        "step_unknowns": {},
        "attempts": [],
        "manual_checks": [],
        "feedback": "先观看示范，再开始操作。",
        "service_error": "",
    }


def reset_confirmation(state: dict[str, Any]) -> None:
    state.update(confirm_count=0, confirm_hashes=[], confirm_sequence=0)


def complete_step(
    state: dict[str, Any],
    steps: list[SopStep],
    now_ms: int,
    *,
    skipped: bool = False,
) -> dict[str, Any]:
    result = deepcopy(state)
    index = result["current_step_index"]
    bucket = "skipped" if skipped else "completed"
    result[bucket] = [*result[bucket], index]
    result["attempts"].append(
        {
            "step_index": index,
            "duration_ms": result.get("step_active_ms", 0)
            + max(0, now_ms - result["entered_at_ms"]),
            "source": "optional_skip" if skipped else "visual_verified",
            "completed_at_ms": now_ms,
        }
    )
    done = set(result["completed"]) | set(result["skipped"])
    candidates = [
        s
        for s in sorted(steps, key=lambda s: s.index)
        if s.index not in done and set(s.predecessors) <= done
    ]
    reset_confirmation(result)
    result.update(
        step_revision=result["step_revision"] + 1,
        checks=[],
        manual_checks=[],
        pending_observation_id=None,
        service_error="",
        unknown_streak=0,
    )
    if candidates:
        result.update(
            current_step_index=candidates[0].index,
            entered_at_ms=now_ms,
            step_active_ms=0,
            step_state="observing",
            feedback="上一步已完成，请观看下一步示范。",
        )
    elif len(done) == len(steps):
        result.update(current_step_index=None, step_state="completed", feedback="全部步骤完成。")
    else:
        raise ValueError("未完成步骤的前置条件无法满足")
    return result


def apply_verification(
    state: dict[str, Any],
    steps: list[SopStep],
    checks: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    now_ms: int,
) -> tuple[dict[str, Any], str]:
    result = deepcopy(state)
    step = next(s for s in steps if s.index == result["current_step_index"])
    assert step.verification is not None
    result.update(checks=checks, pending_observation_id=None, service_error="")
    by_id = {check["check_id"]: check for check in checks}
    required = [c for c in step.verification.checks if c.required]
    violations = [c for c in required if by_id.get(c.id, {}).get("status") == "violated"]
    if violations:
        reset_confirmation(result)
        result["step_state"] = "correcting"
        result["error_count"] += int(state["step_state"] != "correcting")
        if state["step_state"] != "correcting":
            key = str(step.index)
            result.setdefault("step_errors", {})[key] = (
                result.get("step_errors", {}).get(key, 0) + 1
            )
        result["unknown_streak"] = 0
        check = violations[0]
        result["feedback"] = check.correction or f"请对照标准图修正：{check.criterion}。"
        return result, "feedback"
    visual = [c for c in required if c.mode == "visual"]
    manual_ready = all(c.id in result["manual_checks"] for c in required if c.mode == "manual")
    visible_ready = any(c.category == "completion" for c in visual) and all(
        by_id.get(c.id, {}).get("status") == "satisfied" for c in visual
    )
    done = set(result["completed"]) | set(result["skipped"])
    if not (required and visible_ready and manual_ready and set(step.predecessors) <= done):
        reset_confirmation(result)
        result["step_state"] = "observing"
        unknown = any(by_id.get(c.id, {}).get("status", "unknown") == "unknown" for c in visual)
        if unknown:
            key = str(step.index)
            result["unknown_streak"] = result.get("unknown_streak", 0) + 1
            result.setdefault("step_unknowns", {})[key] = (
                result.get("step_unknowns", {}).get(key, 0) + 1
            )
        else:
            result["unknown_streak"] = 0
        result["feedback"] = (
            "请移开手或调整视角，让必要条件清楚可见。"
            if unknown
            else "还有必要条件待教师确认。"
            if visible_ready and not manual_ready
            else "正在操作，完成后请让结果保持可见。"
        )
        return result, "verification_result"
    # Only evidence actually cited for required checks may confirm completion.
    cited = {fid for c in visual for fid in by_id[c.id]["evidence_frame_ids"]}
    result["unknown_streak"] = 0
    evidence = [f for f in frames if f["id"] in cited]
    if step.verification.confirm_windows == 1:
        evidence_times = [f.get("captured_at_ms", 0) for f in evidence]
        evidence_hashes = {f["sha256"] for f in evidence}
        evidence_span_ms = max(evidence_times, default=0) - min(evidence_times, default=0)
        if len(evidence_hashes) < 2 or evidence_span_ms < 800:
            result.update(
                step_state="confirming",
                feedback="完成状态只在单帧中可见，正在自动补拍多帧确认。",
            )
            return result, "verification_result"
    newest = max((f["sequence"] for f in evidence), default=0)
    new_hashes = {f["sha256"] for f in evidence} - set(result["confirm_hashes"])
    if not new_hashes or newest <= result["confirm_sequence"]:
        result["feedback"] = "等待新的操作画面确认完成。"
        return result, "verification_result"
    result["confirm_count"] += 1
    result["confirm_hashes"] = sorted(set(result["confirm_hashes"]) | new_hashes)
    result["confirm_sequence"] = newest
    result.update(step_state="confirming", feedback="完成条件已满足，正在用新画面确认。")
    if result["confirm_count"] >= step.verification.confirm_windows:
        return complete_step(result, steps, now_ms), "step_changed"
    return result, "verification_result"
