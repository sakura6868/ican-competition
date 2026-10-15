"""视觉后端对比评估：同一视频关键帧分别跑 A（onnx）与 C（qwen）两路径。

用法：uv run python -m gongxu.vision.compare --asset <asset_id> [--backends onnx,qwen]

输出 reports/generated/vision-backend-compare.json（per-frame 检测、质量门控、
对齐 SOP 步骤判定、耗时），并打印摘要。结论是选型证据：两路径均为真实可运行
推理，但准确率未在真实角磨机数据上 validated。
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
from gongxu.media.video import extract_keyframes
from gongxu.sop.fixtures import grinder_disassembly_sop
from gongxu.vision import align, facts
from gongxu.vision.adapters import AdapterUnavailableError, VisionAdapter
from gongxu.vision.facts import Detection

_MAX_KEYFRAMES = 24
_REPORT = PROJECT_ROOT / "reports" / "generated"


def _get_db() -> Session:
    init_db()
    return get_session_factory()()


def _det_to_dict(d: Detection) -> dict[str, Any]:
    return {
        "timestamp_ms": d.timestamp_ms,
        "hand": d.hand,
        "tool": d.tool,
        "object": d.object,
        "action": d.action,
        "relation": d.relation,
        "confidence": round(d.confidence, 3),
    }


def _make_adapter(backend: str) -> VisionAdapter:
    if backend == "onnx":
        from gongxu.vision.adapters_onnx import OnnxVisionAdapter

        return OnnxVisionAdapter()
    if backend == "qwen":
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        return QwenVisionAdapter()
    raise ValueError(f"未知视觉后端: {backend}")


def _run_backend(
    backend: str, frames: list[Path]
) -> dict[str, Any]:
    result: dict[str, Any] = {"backend": backend}
    try:
        adapter = _make_adapter(backend)
        result["model_version"] = adapter.model_version()
    except (AdapterUnavailableError, ValueError) as exc:
        result["ok"] = False
        result["error"] = str(exc)
        return result

    detections: list[Detection] = []
    per_frame: list[list[dict[str, Any]]] = []
    latencies: list[float] = []
    for i, frame_path in enumerate(frames):
        start = time.perf_counter()
        try:
            dets = adapter.infer_frame(frame_path.read_bytes(), timestamp_ms=int(i * 1000))
        except AdapterUnavailableError as exc:
            result["ok"] = False
            result["error"] = f"第 {i + 1} 帧推理失败: {exc}"
            if latencies:
                result["latency_ms_avg"] = round(sum(latencies) / len(latencies), 1)
            return result
        latencies.append((time.perf_counter() - start) * 1000)
        detections.extend(dets)
        per_frame.append([_det_to_dict(d) for d in dets])
        print(f"  [{backend}] 帧 {i + 1}/{len(frames)} → {len(dets)} 检测")

    ok, reason = facts.quality_gate(detections, total_frames=len(frames))
    observations = facts.build_observations(detections)
    sop = grinder_disassembly_sop()
    alignment = align.align_observations(sop.steps, observations)
    result.update(
        {
            "ok": ok,
            "quality_reason": reason,
            "total_detections": len(detections),
            "visible_ratio": facts.visible_frame_ratio(detections, len(frames)),
            "observations": len(observations),
            "detections_per_frame": per_frame,
            "latency_ms_avg": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "matched_steps": alignment.matched_observation_count,
            "steps": [
                {
                    "index": r.step.index,
                    "judgement": r.judgement,
                    "rule_code": r.rule_code,
                    "reason": r.reason,
                }
                for r in alignment.step_results
            ],
        }
    )
    return result


def cmd_compare(args: argparse.Namespace) -> int:
    db = _get_db()
    settings = get_settings()
    try:
        if not args.asset:
            print("错误：需要 --asset <asset_id>（或先提供加密视频资产）", file=sys.stderr)
            return 1
        asset = db.get(SourceAsset, args.asset)
        if asset is None or asset.kind != "video":
            print(f"错误：资产不存在或不是视频: {args.asset}", file=sys.stderr)
            return 1

        backends = [b.strip() for b in args.backends.split(",") if b.strip()]
        work_root = settings.data_root / "runtime" / "vision-compare"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{asset.id[:8]}_", dir=work_root) as tmpdir:
            tmp = Path(tmpdir)
            video_path = asset_service.decrypt_to_temp(db, asset.id, temp_dir=tmp / "plain")
            frames_dir = tmp / "frames"
            extract_keyframes(video_path, frames_dir, fps=1.0, max_frames=_MAX_KEYFRAMES)
            frames = sorted(frames_dir.glob("*.jpg"))
            if not frames:
                print("错误：视频未抽出关键帧", file=sys.stderr)
                return 1
            print(f"资产 {asset.filename}：抽到 {len(frames)} 帧，运行后端 {backends}")
            results = [_run_backend(b, frames) for b in backends]

        report = {
            "asset_id": asset.id,
            "filename": asset.filename,
            "keyframes": len(frames),
            "generated_at": datetime.now(UTC).isoformat(),
            "honesty": (
                "两路径均为真实可运行推理，但准确率未在真实角磨机数据上 validated；"
                "onnx 对工序词表工具覆盖弱（COCO 无 wrench/screwdriver），qwen 每帧外部调用。"
            ),
            "backends": {r["backend"]: r for r in results},
        }
        _REPORT.mkdir(parents=True, exist_ok=True)
        out = _REPORT / "vision-backend-compare.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n对比报告已写入: {out}")
        _print_summary(report)
        return 0
    finally:
        db.close()


def _print_summary(report: dict[str, Any]) -> None:
    print("\n=== 对比摘要 ===")
    for backend, r in report["backends"].items():
        if not r.get("ok"):
            print(f"[{backend}] 失败: {r.get('error')}")
            continue
        judged = [s for s in r["steps"] if s["judgement"] == "passed"]
        print(
            f"[{backend}] 模型={r.get('model_version')} 帧均耗={r.get('latency_ms_avg')}ms "
            f"检测={r['total_detections']} 观察={r['observations']} 通过步骤={len(judged)}/"
            f"{len(r['steps'])} 质量门控={'通过' if r['ok'] else '拒判: ' + r['quality_reason']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gongxu.vision.compare", description="A/C 视觉后端对比评估"
    )
    parser.add_argument("--asset", default="", help="加密视频资产 id")
    parser.add_argument("--backends", default="onnx,qwen", help="逗号分隔的后端列表")
    args = parser.parse_args()
    return cmd_compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
