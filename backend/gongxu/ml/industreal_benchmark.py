"""Build a local-only IndustReal development-validation case manifest.

The builder consumes records that have already crossed the integrity boundary in
``gongxu.data.industreal_adapter``.  It never reads video bytes, extracts frames,
or calls a model/provider.  Official ``val`` recordings are useful for pipeline
development, but are deliberately not represented as a final test set and cannot
support a formal accuracy claim.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gongxu.config import PROJECT_ROOT, get_settings
from gongxu.data.industreal_adapter import (
    CompletionStatus,
    IndustRealPilot,
    IndustRealRecord,
    load_industreal_pilot,
)

DEFAULT_PRE_ROLL_MS = 2_000
DEFAULT_POST_ROLL_MS = 1_000
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class IndustRealBenchmarkError(ValueError):
    """Raised when a trustworthy development benchmark cannot be built or loaded."""


class IndustRealBenchmarkCase(BaseModel):
    """One local media reference and one completion-event ground truth."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    dataset_id: Literal["industreal"] = "industreal"
    dataset_version: str
    split: Literal["val"] = "val"
    evaluation_role: Literal["development_validation"] = "development_validation"
    recording_id: str
    participant_id: str
    media_sha256: str = Field(pattern=_SHA256_PATTERN)
    step_id: int = Field(ge=0)
    source_frame: int = Field(ge=0)
    event_timestamp_ms: int = Field(ge=0)
    event_end_ms: int = Field(gt=0)
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(gt=0)
    ground_truth_status: CompletionStatus
    ground_truth_label: str
    external_provider_export: Literal["blocked"] = "blocked"
    raw_frames_included: Literal[False] = False

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if not self.case_id.strip() or not self.recording_id.strip():
            raise ValueError("case_id and recording_id must be non-empty")
        if not self.participant_id.strip() or not self.ground_truth_label.strip():
            raise ValueError("participant_id and ground_truth_label must be non-empty")
        if self.event_end_ms <= self.event_timestamp_ms:
            raise ValueError("event interval must be non-empty")
        if not (
            self.window_start_ms
            <= self.event_timestamp_ms
            < self.event_end_ms
            <= self.window_end_ms
        ):
            raise ValueError("analysis window must contain the complete event interval")
        return self


class IndustRealBenchmarkSummary(BaseModel):
    """Counts derived from cases rather than manually entered claims."""

    model_config = ConfigDict(frozen=True)

    recording_count: int = Field(ge=1)
    participant_count: int = Field(ge=1)
    case_count: int = Field(ge=1)
    ground_truth_distribution: dict[CompletionStatus, int]


class IndustRealBenchmarkManifest(BaseModel):
    """Portable, provider-neutral manifest for development evaluation only."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["industreal-development-benchmark-v1"] = (
        "industreal-development-benchmark-v1"
    )
    dataset_id: Literal["industreal"] = "industreal"
    dataset_version: str
    selected_split: Literal["val"] = "val"
    evaluation_role: Literal["development_validation"] = "development_validation"
    formal_accuracy_claim_allowed: Literal[False] = False
    external_provider_export: Literal["blocked"] = "blocked"
    external_provider_calls: Literal[0] = 0
    raw_frames_included: Literal[False] = False
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    context_pre_roll_ms: int = Field(ge=0)
    context_post_roll_ms: int = Field(ge=0)
    summary: IndustRealBenchmarkSummary
    cases: list[IndustRealBenchmarkCase]

    @model_validator(mode="after")
    def validate_derived_fields(self) -> Self:
        if not self.cases:
            raise ValueError("development benchmark must contain at least one val case")
        if any(case.dataset_version != self.dataset_version for case in self.cases):
            raise ValueError("all cases must use the manifest dataset version")
        expected = _summarise_cases(self.cases)
        if self.summary != expected:
            raise ValueError("summary does not match benchmark cases")
        return self


def _participant_id(recording_id: str) -> str:
    """IndustReal recording names start with the anonymised participant token."""

    participant = recording_id.partition("_")[0].strip()
    if not participant:
        raise IndustRealBenchmarkError(
            f"cannot derive participant id from recording: {recording_id!r}"
        )
    return participant


def _summarise_cases(
    cases: Sequence[IndustRealBenchmarkCase],
) -> IndustRealBenchmarkSummary:
    correct = sum(case.ground_truth_status == "correct" for case in cases)
    incorrect = sum(case.ground_truth_status == "incorrect" for case in cases)
    return IndustRealBenchmarkSummary(
        recording_count=len({case.recording_id for case in cases}),
        participant_count=len({case.participant_id for case in cases}),
        case_count=len(cases),
        ground_truth_distribution={"correct": correct, "incorrect": incorrect},
    )


def _case_id(
    *,
    dataset_version: str,
    record: IndustRealRecord,
    frame: int,
    step_id: int,
    status: CompletionStatus,
    label: str,
    occurrence: int,
) -> str:
    identity = "\x1f".join(
        (
            "industreal",
            dataset_version,
            record.recording_id,
            record.media_sha256,
            str(frame),
            str(step_id),
            status,
            label,
            str(occurrence),
        )
    )
    return f"industreal-val-{sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def build_industreal_benchmark(
    source: IndustRealPilot | Path,
    *,
    context_pre_roll_ms: int = DEFAULT_PRE_ROLL_MS,
    context_post_roll_ms: int = DEFAULT_POST_ROLL_MS,
) -> IndustRealBenchmarkManifest:
    """Build deterministic cases from verified ``val`` completion events only.

    ``source`` may be an already validated adapter object or a pilot directory.
    Passing a directory invokes :func:`load_industreal_pilot`, including all file
    size/hash/pairing checks.  Video content is never opened by this module.
    """

    if context_pre_roll_ms < 0 or context_post_roll_ms < 0:
        raise IndustRealBenchmarkError("context window values must be non-negative")
    pilot = load_industreal_pilot(source) if isinstance(source, Path) else source
    val_records = sorted(
        (record for record in pilot.records if record.split == "val"),
        key=lambda record: record.recording_id,
    )
    if not val_records:
        raise IndustRealBenchmarkError(
            "no val recordings are available; train/test must not substitute for "
            "development validation"
        )

    cases: list[IndustRealBenchmarkCase] = []
    for record in val_records:
        ordered_events = sorted(
            record.completion_events,
            key=lambda event: (
                event.frame,
                event.step_id,
                event.status,
                event.label,
            ),
        )
        occurrence_counts: dict[tuple[int, int, str, str], int] = {}
        for event in ordered_events:
            identity = (event.frame, event.step_id, event.status, event.label)
            occurrence = occurrence_counts.get(identity, 0)
            occurrence_counts[identity] = occurrence + 1
            cases.append(
                IndustRealBenchmarkCase(
                    case_id=_case_id(
                        dataset_version=pilot.dataset_version,
                        record=record,
                        frame=event.frame,
                        step_id=event.step_id,
                        status=event.status,
                        label=event.label,
                        occurrence=occurrence,
                    ),
                    dataset_version=pilot.dataset_version,
                    recording_id=record.recording_id,
                    participant_id=_participant_id(record.recording_id),
                    media_sha256=record.media_sha256.lower(),
                    step_id=event.step_id,
                    source_frame=event.frame,
                    event_timestamp_ms=event.timestamp_ms,
                    event_end_ms=event.end_ms,
                    window_start_ms=max(0, event.timestamp_ms - context_pre_roll_ms),
                    window_end_ms=event.end_ms + context_post_roll_ms,
                    ground_truth_status=event.status,
                    ground_truth_label=event.label,
                )
            )
    if not cases:
        raise IndustRealBenchmarkError("val recordings contain no completion events")

    cases.sort(key=lambda case: (case.recording_id, case.event_timestamp_ms, case.case_id))
    return IndustRealBenchmarkManifest(
        dataset_version=pilot.dataset_version,
        source_manifest_sha256=pilot.manifest_sha256.lower(),
        context_pre_roll_ms=context_pre_roll_ms,
        context_post_roll_ms=context_post_roll_ms,
        summary=_summarise_cases(cases),
        cases=cases,
    )


def load_benchmark_manifest(path: Path) -> IndustRealBenchmarkManifest:
    """Load the versioned Pydantic manifest for a future local benchmark runner."""

    try:
        return IndustRealBenchmarkManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise IndustRealBenchmarkError(f"cannot load benchmark manifest: {exc}") from exc


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_benchmark_markdown(manifest: IndustRealBenchmarkManifest) -> str:
    """Render an auditable human-readable companion without local media paths."""

    distribution = manifest.summary.ground_truth_distribution
    lines = [
        "# IndustReal Development Benchmark",
        "",
        "> 本清单仅用于开发验证，不是独立测试集，不允许据此宣称正式准确率。",
        "",
        "## 边界",
        "",
        "- 数据划分：`val`（development validation）",
        "- 正式准确率声明：`false`",
        "- 外部 Provider 外发：`blocked`",
        "- 外部 Provider 调用：`0`",
        "- 原始帧包含：`false`",
        "",
        "## 汇总",
        "",
        f"- 数据版本：`{_markdown_cell(manifest.dataset_version)}`",
        f"- Recording 数：`{manifest.summary.recording_count}`",
        f"- 参与者数：`{manifest.summary.participant_count}`",
        f"- Case 数：`{manifest.summary.case_count}`",
        f"- 正确事件：`{distribution['correct']}`",
        f"- 错误事件：`{distribution['incorrect']}`",
        f"- 来源 manifest SHA256：`{manifest.source_manifest_sha256}`",
        "",
        "## Cases",
        "",
        "| Case | Recording | 参与者 | 时间戳 ms | 窗口 [ms) | 状态 | 标签 | Media SHA256 |",
        "|---|---|---|---:|---:|---|---|---|",
    ]
    for case in manifest.cases:
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(case.case_id),
                    _markdown_cell(case.recording_id),
                    _markdown_cell(case.participant_id),
                    str(case.event_timestamp_ms),
                    f"[{case.window_start_ms}, {case.window_end_ms})",
                    case.ground_truth_status,
                    _markdown_cell(case.ground_truth_label),
                    case.media_sha256,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_benchmark_outputs(
    manifest: IndustRealBenchmarkManifest,
    *,
    json_path: Path,
    markdown_path: Path,
) -> tuple[Path, Path]:
    """Write deterministic JSON and Markdown representations."""

    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_benchmark_markdown(manifest), encoding="utf-8")
    return json_path, markdown_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a local-only IndustReal val development benchmark manifest"
    )
    parser.add_argument(
        "--pilot-root",
        type=Path,
        default=get_settings().data_root / "raw" / "industreal-v2" / "pilot",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=PROJECT_ROOT / "reports" / "generated" / "industreal-benchmark.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=PROJECT_ROOT / "reports" / "generated" / "industreal-benchmark.md",
    )
    parser.add_argument("--pre-roll-ms", type=int, default=DEFAULT_PRE_ROLL_MS)
    parser.add_argument("--post-roll-ms", type=int, default=DEFAULT_POST_ROLL_MS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manifest = build_industreal_benchmark(
        args.pilot_root,
        context_pre_roll_ms=args.pre_roll_ms,
        context_post_roll_ms=args.post_roll_ms,
    )
    json_path, markdown_path = write_benchmark_outputs(
        manifest,
        json_path=args.json_output,
        markdown_path=args.markdown_output,
    )
    print(f"benchmark cases: {manifest.summary.case_count}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IndustRealBenchmarkCase",
    "IndustRealBenchmarkError",
    "IndustRealBenchmarkManifest",
    "IndustRealBenchmarkSummary",
    "build_industreal_benchmark",
    "load_benchmark_manifest",
    "main",
    "render_benchmark_markdown",
    "write_benchmark_outputs",
]
