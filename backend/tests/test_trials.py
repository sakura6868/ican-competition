"""试次解析与划分测试。"""

from __future__ import annotations

import json
from pathlib import Path

from gongxu.data.trials import scan_annotations, split_trials_by_participant


def _make_annotation(
    root: Path, identifier: str, *, view: str, segments: int, anomaly: bool = False
) -> None:
    view_dir = root / "AF-S" / "annotations" / view
    view_dir.mkdir(parents=True, exist_ok=True)
    segs = [
        {
            "action_label": i,
            "verb": 1,
            "noun": 1,
            "start_frame": i * 100,
            "end_frame": i * 100 + 50,
            "phase": "normal",
            "anomaly_type": [1, 0, 0, 0, 0, 0] if anomaly else [0, 0, 0, 0, 0, 0],
        }
        for i in range(segments)
    ]
    (view_dir / f"{identifier}.json").write_text(
        json.dumps({"video_id": identifier, "view": view, "segments": segs}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_scan_annotations(tmp_path: Path) -> None:
    _make_annotation(
        tmp_path, "AL07EJ17_Disassembly_A_001_ego", view="ego", segments=75, anomaly=True
    )
    _make_annotation(tmp_path, "AL07EJ17_Disassembly_A_001_front", view="front", segments=75)
    _make_annotation(tmp_path, "ER07AD15_Reassembly_B_002_ego", view="ego", segments=40)

    trials = scan_annotations(tmp_path)
    assert len(trials) == 3
    by_id = {t.identifier: t for t in trials}
    assert by_id["AL07EJ17_Disassembly_A_001_ego"].participant == "AL07EJ17"
    assert by_id["AL07EJ17_Disassembly_A_001_ego"].task == "Disassembly"
    assert by_id["ER07AD15_Reassembly_B_002_ego"].task == "Reassembly"
    assert by_id["AL07EJ17_Disassembly_A_001_ego"].has_anomaly is True


def test_split_by_participant_keeps_together(tmp_path: Path) -> None:
    """同一参与者的多个视角必须进入同一划分（防跨视角泄漏）。"""
    for view in ("ego", "front"):
        _make_annotation(tmp_path, f"AL07EJ17_Disassembly_A_001_{view}", view=view, segments=10)
    _make_annotation(tmp_path, "ER07AD15_Reassembly_B_002_ego", view="ego", segments=10)
    _make_annotation(tmp_path, "KI03AR28_Disassembly_A_001_ego", view="ego", segments=10)

    trials = scan_annotations(tmp_path)
    splits = split_trials_by_participant(trials, seed=42)
    for t in trials:
        member = next(s for s, ids in splits.items() if t.identifier in ids)
        same = [x for x in trials if x.participant == t.participant]
        for x in same:
            member2 = next(s for s, ids in splits.items() if x.identifier in ids)
            assert member == member2, "同一参与者不得跨划分"
