"""Full-coverage overlapping teaching windows and optional faster-whisper narration."""

import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gongxu.config import get_settings
from gongxu.media.video import SampledFrame, _ffmpeg_bin, _run, extract_frame_at, probe
from gongxu.providers.base import (
    MultimodalProvider,
    ProviderError,
    ProviderReceipt,
    ReferenceSegment,
)


def plan_windows(duration_ms: int, size_ms: int, overlap_ms: int = 3000) -> list[tuple[int, int]]:
    if duration_ms <= 0 or size_ms <= overlap_ms or overlap_ms < 0:
        raise ValueError("视频时长或分段窗口配置无效")
    windows = []
    start = 0
    while start < duration_ms:
        end = min(duration_ms, start + size_ms)
        windows.append((start, end))
        if end == duration_ms:
            break
        start = end - overlap_ms
    return windows


def sample_teaching_video(
    video: Path, directory: Path
) -> tuple[list[SampledFrame], list[tuple[int, int]], int]:
    info = probe(video)
    duration = round(info.duration_s * 1000)
    settings = get_settings()
    windows = plan_windows(duration, settings.teaching_window_seconds * 1000)
    if len(windows) > settings.teaching_max_windows:
        raise ValueError("视频超过本机备课窗口上限，请分成完整的子课程或提高窗口上限")
    directory.mkdir(parents=True, exist_ok=True)
    # Seeking after the final frame's PTS returns EOF, even before container duration.
    last_frame_ms = max(0, duration - math.ceil(1000 / (info.fps or 4)) - 50)
    timestamps = sorted(
        {
            min(last_frame_ms, round(start + i * max(0, end - start - 150) / 5))
            for start, end in windows
            for i in range(6)
        }
    )
    samples = []
    for index, stamp in enumerate(timestamps):
        path = extract_frame_at(video, directory / f"teaching_{index:04d}.jpg", timestamp_ms=stamp)
        samples.append(SampledFrame(path=path, frame_index=index, timestamp_ms=stamp))
    return samples, windows, duration


def transcribe(video: Path, directory: Path) -> tuple[list[dict[str, Any]], str]:
    settings = get_settings()
    if not settings.asr_model:
        return [], "disabled_visual_only"
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ProviderError(
            "启用语音转写前，请安装 uv sync --extra media --extra teaching"
        ) from exc
    audio = directory / "narration.wav"
    process = _run(
        [_ffmpeg_bin(), "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)]
    )
    if process.returncode != 0:
        if (
            "does not contain any stream" in process.stderr
            or "matches no streams" in process.stderr
        ):
            return [], "no_audio"
        raise ProviderError("教学视频音频提取失败")
    model = WhisperModel(
        settings.asr_model,
        device="cpu",
        compute_type="int8",
        download_root=str(settings.model_root / "asr"),
    )
    segments, _info = model.transcribe(str(audio), beam_size=3, vad_filter=True)
    return [
        {"start_ms": round(s.start * 1000), "end_ms": round(s.end * 1000), "text": s.text}
        for s in segments
    ], "transcribed"


def analyze_windows(
    provider: MultimodalProvider,
    video: Path,
    source_id: str,
    samples: list[SampledFrame],
    windows: list[tuple[int, int]],
    directory: Path,
    rag_chunks: list[dict[str, Any]],
    callback: Callable[[str, int, str, dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], ProviderReceipt]:
    narration, transcript_status = transcribe(video, directory)
    analyses, receipts = [], []
    for index, (start, end) in enumerate(windows):
        frames = [f for f in samples if start <= f.timestamp_ms < end]
        # Neighboring windows contribute boundary frames; retain full-span sampling.
        if len(frames) > 6:
            frames = [frames[round(i * (len(frames) - 1) / 5)] for i in range(6)]
        speech = "\n".join(
            f"{s['start_ms']}–{s['end_ms']}ms: {s['text']}"
            for s in narration
            if s["start_ms"] < end and s["end_ms"] > start
        )
        segment = ReferenceSegment(
            segment_id=f"{source_id}:window:{index}",
            keyframes=[str(f.path) for f in frames],
            keyframe_timestamps_ms=[f.timestamp_ms for f in frames],
            asr_text=speech,
            rag_chunks=rag_chunks,
            start_ms=start,
            end_ms=end,
            detection_summary={"teaching_window": True, "start_ms": start, "end_ms": end},
        )
        analysis, receipt = provider.analyze_reference_segment(segment)
        analyses.append({"start_ms": start, "end_ms": end, "analysis": analysis})
        receipts.append(receipt)
        if callback:
            callback(
                "vision_analyzing",
                45 + round((index + 1) / len(windows) * 22),
                f"正在理解教学视频（{index + 1}/{len(windows)} 段）",
                {"analyzed_windows": index + 1, "total_windows": len(windows)},
            )
    result = {
        "analysis_format": "teaching_windows_v1",
        "window_analyses": analyses,
        "source_asset_id": source_id,
        "duration_ms": windows[-1][1],
        "transcript_status": transcript_status,
        "coverage": [{"start_ms": a, "end_ms": b} for a, b in windows],
    }

    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    return result, ProviderReceipt(
        provider=provider.name,
        mode=provider.mode,
        model_id=receipts[0].model_id,
        prompt_version="teaching-windows-v1",
        input_digest=digest([r.input_digest for r in receipts]),
        response_digest=digest(result),
        input_tokens=sum(r.input_tokens for r in receipts),
        output_tokens=sum(r.output_tokens for r in receipts),
        latency_ms=sum(r.latency_ms for r in receipts),
    )


def enrich_steps(raw_sop: dict[str, Any], source_id: str, duration_ms: int) -> None:
    for step in raw_sop.get("steps", []):
        teaching = step.get("teaching")
        if not isinstance(teaching, dict):
            raise ProviderError("模型缺少教学步骤的时间边界，请重新编译或调整模型")
        start, end = teaching.get("start_ms"), teaching.get("end_ms")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end <= duration_ms
        ):
            raise ProviderError("模型返回的教学片段时间无效")
        teaching["source_asset_id"] = source_id
        teaching["reference_timestamps_ms"] = [start, max(start, end - 150)]
        checks = []
        for field, category, mode in (
            ("completion_checks", "completion", "visual"),
            ("observable_checks", "safety", "visual"),
            ("unobservable_checks", "completion", "manual"),
        ):
            for index, criterion in enumerate(step.get(field, [])):
                checks.append(
                    {
                        "id": f"{field}_{index + 1}",
                        "criterion": criterion,
                        "category": category,
                        "mode": mode,
                        "required": True,
                        "correction": "",
                    }
                )
        needs_double_confirmation = bool(step.get("critical")) or any(
            check["required"] and check["category"] == "safety" for check in checks
        )
        step["verification"] = {
            "checks": checks,
            "confirm_windows": 2 if needs_double_confirmation else 1,
            "teacher_confirmed": False,
        }
