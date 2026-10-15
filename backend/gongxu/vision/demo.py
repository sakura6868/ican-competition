"""端到端演示：视频 → 抽帧去重 → qwen 批处理 → 观察 → 门控 → 对齐 → 评分。

用法：uv run python -m gongxu.vision.demo --asset <asset_id> [--process <process_key>]

默认使用 IMPACT 齿轮箱演示工序（域匹配，见 gongxu.sop.fixtures）。
输出步骤判定与评分摘要，并存 reports/generated/demo-assessment-<asset>.json。

诚实声明：qwen 为真实推理，但未在受控测试集 validated；演示用于证明
「视频→真实识别→确定性评分」链路有效，不代表生产级判定准确率。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from gongxu.assets import service as asset_service
from gongxu.config import PROJECT_ROOT, get_settings
from gongxu.db.models import SourceAsset
from gongxu.db.session import get_session_factory, init_db
from gongxu.media.video import SampledFrame, extract_keyframes_uniform, probe
from gongxu.scoring.engine import score_assessment
from gongxu.sop.fixtures import (
    gearbox_disassembly_sop,
    grinder_disassembly_sop,
    robot_assemble_sop,
)
from gongxu.vision import align, facts
from gongxu.vision.adapters_qwen import QwenVisionAdapter
from gongxu.vision.facts import Detection

_MAX_KEYFRAMES = 24
_REPORT = PROJECT_ROOT / "reports" / "generated"

_PROCESS_TO_SOP = {
    "impact_gearbox_disassembly_demo": gearbox_disassembly_sop,
    "impact_grinder_safe_subassembly": grinder_disassembly_sop,
    "assembly101_robot_assemble_demo": robot_assemble_sop,
}


def _get_db() -> Session:
    init_db()
    return get_session_factory()()


def _dhash(image_bytes: bytes, size: int = 8) -> int:
    """简单感知哈希：降灰 → 缩小 → 邻域比较位串。"""
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize(
        (size + 1, size), Image.Resampling.BILINEAR
    )
    px = np.asarray(img, dtype=np.uint8)
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (int(px[y, x]) > int(px[y, x + 1]))
    return bits


def _dedupe_frames(
    frames: list[Path], *, hamming: int = 1, min_keep: int = 8
) -> list[Path]:
    """感知哈希去重：差异过小则跳过；去重后不足 min_keep 帧才均匀补帧。

    装配视频相邻帧相似度高但操作段集中，hamming=1 能保留关键操作帧
    （避免均匀补帧错过操作密集段）。
    """
    kept: list[Path] = []
    prev_hash: int | None = None
    for fp in frames:
        h = _dhash(fp.read_bytes())
        if prev_hash is None or bin(h ^ prev_hash).count("1") > hamming:
            kept.append(fp)
            prev_hash = h
    if len(kept) < min_keep and len(frames) > min_keep:
        idxs = sorted(
            {round(i * (len(frames) - 1) / (min_keep - 1)) for i in range(min_keep)}
        )
        kept = [frames[i] for i in idxs]
    return kept


def _det_to_dict(d: Detection | facts.Observation) -> dict[str, Any]:
    return {
        "timestamp_ms": d.timestamp_ms,
        "hand": d.hand,
        "tool": d.tool,
        "object": d.object,
        "action": d.action,
        "relation": d.relation,
        "confidence": round(d.confidence, 3),
    }


def cmd_demo(args: argparse.Namespace) -> int:
    db = _get_db()
    settings = get_settings()
    try:
        asset = db.get(SourceAsset, args.asset)
        if asset is None or asset.kind != "video":
            print(f"错误：资产不存在或不是视频: {args.asset}", file=sys.stderr)
            return 1
        sop_fn = _PROCESS_TO_SOP.get(args.process)
        if sop_fn is None:
            print(f"错误：未知工序 {args.process}；可选: {list(_PROCESS_TO_SOP)}", file=sys.stderr)
            return 1
        sop = sop_fn()

        try:
            adapter = QwenVisionAdapter(process_key=sop.process_key)
            configure_steps = getattr(adapter, "set_sop_steps", None)
            if callable(configure_steps):
                configure_steps(sop.steps)
        except Exception as exc:
            print(f"错误：qwen 适配器不可用: {exc}", file=sys.stderr)
            return 1

        work_root = settings.data_root / "runtime" / "demo"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{asset.id[:8]}_", dir=work_root) as tmpdir:
            tmp = Path(tmpdir)
            video_path = asset_service.decrypt_to_temp(db, asset.id, temp_dir=tmp / "plain")
            frames_dir = tmp / "frames"
            extracted = extract_keyframes_uniform(
                video_path, frames_dir, count=_MAX_KEYFRAMES
            )
            info = probe(video_path)
            raw_frames = _normalize_demo_frames(extracted, frames_dir, info.duration_s)
            kept_paths = _dedupe_frames([frame.path for frame in raw_frames])
            kept = set(kept_paths)
            frames = [frame for frame in raw_frames if frame.path in kept]
            if not frames:
                print("错误：视频未抽出关键帧", file=sys.stderr)
                return 1
            print(
                f"资产 {asset.filename}：抽到 {len(raw_frames)} 帧，去重后 {len(frames)} 帧"
                f"，工序 {sop.process_key}（{len(sop.steps)} 步）"
            )

            batches = [frames[i : i + 4] for i in range(0, len(frames), 4)]
            detections: list[Detection] = []
            latencies: list[float] = []
            for batch_frames in batches:
                items = [
                    (frame.path.read_bytes(), frame.timestamp_ms) for frame in batch_frames
                ]
                start = time.perf_counter()
                per_frame = adapter.infer_batch(items)
                latencies.append((time.perf_counter() - start) * 1000)
                for sampled, dets in zip(batch_frames, per_frame, strict=False):
                    for detection in dets:
                        detection.frame_index = sampled.frame_index
                    detections.extend(dets)
                print(f"  批 {len(items)} 帧 → {sum(len(d) for d in per_frame)} 检测 "
                      f"({int(latencies[-1])}ms)")

        ok, reason = facts.quality_gate(detections, total_frames=len(frames))
        observations = facts.build_observations(detections)
        alignment = align.align_observations(sop.steps, observations)
        result = score_assessment(sop.steps, alignment.step_results, sop.scoring_profile)

        print("\n=== 演示评估摘要 ===")
        print(f"可见比例={facts.visible_frame_ratio(detections, len(frames))} "
              f"门控={'通过' if ok else '拒判: ' + reason}")
        print(f"检测={len(detections)} 观察={len(observations)} "
              f"批均耗时={int(sum(latencies) / len(latencies)) if latencies else 0}ms")
        print(f"总分={result.total_score} 结果={result.result} "
              f"证据覆盖={result.evidence_coverage}")
        print("\n步骤判定:")
        for r in alignment.step_results:
            print(f"  step{r.step.index} {r.step.name} → {r.judgement} "
                  f"({r.rule_code}) {r.reason}")

        report = {
            "asset_id": asset.id,
            "filename": asset.filename,
            "process_key": sop.process_key,
            "frames_raw": len(raw_frames),
            "frames_used": len(frames),
            "sampled_timestamps_ms": [frame.timestamp_ms for frame in frames],
            "generated_at": datetime.now(UTC).isoformat(),
            "quality": {"ok": ok, "reason": reason,
                        "visible_ratio": facts.visible_frame_ratio(detections, len(frames))},
            "batch_avg_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
            "scoring": {
                "total_score": result.total_score,
                "result": result.result,
                "dimension_scores": result.dimension_scores,
                "evidence_coverage": result.evidence_coverage,
            },
            "steps": [
                {"index": r.step.index, "name": r.step.name, "judgement": r.judgement,
                 "rule_code": r.rule_code, "reason": r.reason}
                for r in alignment.step_results
            ],
            "observations": [_det_to_dict(o) for o in observations],
            "honesty": (
                "qwen 为真实推理但未 validated；演示用于证明链路有效，"
                "不代表生产级判定准确率。"
            ),
        }
        _REPORT.mkdir(parents=True, exist_ok=True)
        out = _REPORT / f"demo-assessment-{asset.id[:8]}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {out}")
        return 0
    finally:
        db.close()


def _normalize_demo_frames(
    extracted: list[SampledFrame] | None,
    frames_dir: Path,
    duration_s: float,
) -> list[SampledFrame]:
    """将旧抽帧插件返回的路径补为全局时间帧。"""
    if extracted:
        return sorted(extracted, key=lambda frame: (frame.timestamp_ms, frame.frame_index))
    paths = sorted(frames_dir.glob("*.jpg"))
    duration_ms = max(0, round(duration_s * 1000))
    return [
        SampledFrame(
            path=path,
            frame_index=i,
            timestamp_ms=(
                round(i * duration_ms / max(len(paths) - 1, 1))
                if len(paths) > 1
                else 0
            ),
        )
        for i, path in enumerate(paths)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(prog="gongxu.vision.demo", description="端到端演示评估")
    parser.add_argument("--asset", required=True, help="加密视频资产 id")
    parser.add_argument(
        "--process",
        default="impact_gearbox_disassembly_demo",
        help="演示工序（决定词表与 SOP）",
    )
    args = parser.parse_args()
    return cmd_demo(args)


if __name__ == "__main__":
    raise SystemExit(main())
