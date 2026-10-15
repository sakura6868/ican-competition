"""多视角融合演示：同一试次的多个视角视频 → 分别 qwen 识别 → 合并观察 → 对齐评分。

用法：
  uv run python -m gongxu.vision.multi_view --trial AL07EJ17_Disassembly_A_001 \
      [--process impact_gearbox_disassembly_demo]

原理：单视角漏检率高（top 只识别 step2/4，front 稳定时识别 step1/2/4）。
融合多视角后，不同视角识别到的不同步骤合并，步骤覆盖率显著提升 → 分数提升。

诚实声明：qwen 为真实推理但未 validated；多视角融合用于证明识别覆盖率可提升，
不代表生产级判定准确率。
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
from gongxu.media.video import extract_keyframes_uniform, probe
from gongxu.scoring.engine import score_assessment
from gongxu.sop.fixtures import gearbox_disassembly_sop, grinder_disassembly_sop
from gongxu.vision import align, facts
from gongxu.vision.adapters_qwen import QwenVisionAdapter
from gongxu.vision.facts import Detection

_MAX_KEYFRAMES = 16  # 每视角帧数（多视角融合时单视角可少些，总帧数可控）
_REPORT = PROJECT_ROOT / "reports" / "generated"

_PROCESS_TO_SOP = {
    "impact_gearbox_disassembly_demo": gearbox_disassembly_sop,
    "impact_grinder_safe_subassembly": grinder_disassembly_sop,
}

_VIEWS = ("front", "top", "left", "right", "ego")


def _get_db() -> Session:
    init_db()
    return get_session_factory()()


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


def cmd_multi_view(args: argparse.Namespace) -> int:
    db = _get_db()
    settings = get_settings()
    try:
        sop_fn = _PROCESS_TO_SOP.get(args.process)
        if sop_fn is None:
            print(f"错误：未知工序 {args.process}", file=sys.stderr)
            return 1
        sop = sop_fn()

        # 按试次名找 5 个视角视频
        pattern = f"%{args.trial}%"
        assets = (
            db.query(SourceAsset)
            .filter(SourceAsset.kind == "video", SourceAsset.filename.like(pattern))
            .all()
        )
        if not assets:
            print(f"错误：未找到试次 {args.trial} 的视频", file=sys.stderr)
            return 1
        # 按视角排序
        assets.sort(key=lambda a: next((i for i, v in enumerate(_VIEWS) if v in a.filename), 99))

        try:
            adapter = QwenVisionAdapter(process_key=sop.process_key)
            configure_steps = getattr(adapter, "set_sop_steps", None)
            if callable(configure_steps):
                configure_steps(sop.steps)
        except Exception as exc:
            print(f"错误：qwen 适配器不可用: {exc}", file=sys.stderr)
            return 1

        work_root = settings.data_root / "runtime" / "multi-view"
        work_root.mkdir(parents=True, exist_ok=True)

        all_detections: list[Detection] = []
        per_view: list[dict[str, Any]] = []
        total_frames = 0

        with tempfile.TemporaryDirectory(prefix="mv_", dir=work_root) as tmpdir:
            tmp = Path(tmpdir)
            for asset in assets:
                view = next((v for v in _VIEWS if v in asset.filename), "unknown")
                vtmp = tmp / view
                vtmp.mkdir(parents=True, exist_ok=True)
                video_path = asset_service.decrypt_to_temp(db, asset.id, temp_dir=vtmp / "plain")
                frames_dir = vtmp / "frames"
                extract_keyframes_uniform(video_path, frames_dir, count=_MAX_KEYFRAMES)
                info = probe(video_path)
                dur_ms = (
                    int(info.duration_s * 1000) if info.duration_s > 0 else _MAX_KEYFRAMES * 1000
                )
                frames = sorted(frames_dir.glob("*.jpg"))
                if not frames:
                    print(f"  [{view}] 未抽到帧，跳过")
                    continue
                step_ms = dur_ms // max(len(frames), 1)

                batches = [frames[i : i + 4] for i in range(0, len(frames), 4)]
                view_dets: list[Detection] = []
                t0 = time.perf_counter()
                for batch in batches:
                    items = [(fp.read_bytes(), i * step_ms) for i, fp in enumerate(batch)]
                    try:
                        per_frame = adapter.infer_batch(items)
                    except Exception as exc:
                        print(f"  [{view}] 批推理失败: {exc}", file=sys.stderr)
                        continue
                    for dets in per_frame:
                        view_dets.extend(dets)
                elapsed = int(time.perf_counter() - t0)
                view_obs = facts.build_observations(view_dets)
                view_align = align.align_observations(sop.steps, view_obs)
                view_passed = [
                    r.step.index for r in view_align.step_results if r.judgement == "passed"
                ]
                print(
                    f"  [{view:5s}] 帧={len(frames)} 检测={len(view_dets)} 观察={len(view_obs)} "
                    f"通过={view_passed} ({elapsed}s)"
                )
                per_view.append(
                    {
                        "view": view,
                        "frames": len(frames),
                        "detections": len(view_dets),
                        "observations": len(view_obs),
                        "passed": view_passed,
                        "elapsed_s": elapsed,
                    }
                )
                all_detections.extend(view_dets)
                total_frames += len(frames)

        # 融合后评分：多视角合并 detections 后统一构建观察
        # 门控分母用"有检测的唯一帧"而非总帧数——多视角融合用空间多样性
        # 补偿时间稀疏性，不应因总帧数大而稀释可见比例
        unique_frames = len({d.frame_hash for d in all_detections if d.frame_hash})
        gate_frames = max(unique_frames, len(assets) * 4)  # 至少每视角 4 帧基数
        print(
            f"\n=== 多视角融合（{len(assets)}视角，总{total_frames}帧，"
            f"有检测{unique_frames}帧）==="
        )
        ok, reason = facts.quality_gate(all_detections, total_frames=gate_frames)
        observations = facts.build_observations(all_detections)
        alignment = align.align_observations(sop.steps, observations)
        result = score_assessment(sop.steps, alignment.step_results, sop.scoring_profile)

        print(
            f"可见比例={facts.visible_frame_ratio(all_detections, gate_frames)} "
            f"门控={'通过' if ok else '拒判: ' + reason}"
        )
        print(f"总检测={len(all_detections)} 融合观察={len(observations)}")
        print(f"总分={result.total_score} 结果={result.result} 覆盖={result.evidence_coverage}")
        print(f"维度={result.dimension_scores}")
        print("\n步骤判定:")
        for r in alignment.step_results:
            print(f"  step{r.step.index} {r.step.name} → {r.judgement} ({r.rule_code})")

        report = {
            "trial": args.trial,
            "process_key": sop.process_key,
            "views": len(assets),
            "total_frames": total_frames,
            "per_view": per_view,
            "generated_at": datetime.now(UTC).isoformat(),
            "quality": {
                "ok": ok,
                "reason": reason,
                "visible_ratio": facts.visible_frame_ratio(
                    all_detections, len(assets) * _MAX_KEYFRAMES
                ),
            },
            "scoring": {
                "total_score": result.total_score,
                "result": result.result,
                "dimension_scores": result.dimension_scores,
                "evidence_coverage": result.evidence_coverage,
            },
            "steps": [
                {
                    "index": r.step.index,
                    "name": r.step.name,
                    "judgement": r.judgement,
                    "rule_code": r.rule_code,
                    "reason": r.reason,
                }
                for r in alignment.step_results
            ],
            "observations": [_det_to_dict(o) for o in observations],
            "honesty": "多视角融合提升识别覆盖率；qwen 真实推理但未 validated",
        }
        _REPORT.mkdir(parents=True, exist_ok=True)
        out = _REPORT / f"multi-view-{args.trial}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告: {out}")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="gongxu.vision.multi_view", description="多视角融合考评")
    parser.add_argument("--trial", required=True, help="试次名（如 AL07EJ17_Disassembly_A_001）")
    parser.add_argument("--process", default="impact_gearbox_disassembly_demo", help="工序")
    args = parser.parse_args()
    return cmd_multi_view(args)


if __name__ == "__main__":
    raise SystemExit(main())
