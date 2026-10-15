"""Tests for the read-only local dataset readiness audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gongxu.data.audit import audit_datasets, main, render_markdown, write_reports


def _write(path: Path, content: str | bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def _build_dataset_tree(base: Path) -> Path:
    raw = base / "raw"

    impact = raw / "impact-v1.1" / "IMPACT-v1.1"
    _write(
        impact / "README.md",
        "Official IMPACT release: https://github.com/Kratos-Wen/IMPACT\n"
        "License: CC BY-NC-SA 4.0\n",
    )
    _write(
        impact
        / "annotations"
        / "TAS-S"
        / "front"
        / "AL07_Disassembly_3_1.json",
        json.dumps(
            {
                "participant_id": "AL07",
                "segments": [{"action": "remove filter"}, {"anomaly_type": "omission"}],
            }
        ),
    )
    _write(
        impact / "sample" / "videos" / "front" / "AL07_Disassembly_3_1.mp4"
    )

    industreal = raw / "industreal-official"
    _write(
        industreal / "README.md",
        "IndustReal dataset: https://github.com/TimSchoonbeek/IndustReal\n"
        "License: Apache-2.0\n",
    )
    recording = raw / "industreal-v2" / "recordings" / "01_assy_1_1"
    _write(recording / "rgb" / "000001.jpg")
    _write(
        recording / "PSR_labels_with_errors.csv",
        "000198.jpg,3,Install front chassis\n"
        "000557.jpg,9,Install rear chassis\n",
    )

    assembly101 = raw / "assembly101-mistake-annotations"
    _write(
        assembly101 / "README.md",
        "Mistake Detection benchmark of Assembly101\n"
        "https://github.com/assembly-101/assembly101-mistake-detection\n"
        "License: CC BY-NC 4.0\n",
    )
    _write(
        assembly101
        / "annotations"
        / "nusar-2021_action_both_9033-c02a_9033_user_id_2021-02-01.csv",
        "0,1,pick,filter,frame,correct,ok\n"
        "1,2,insert,filter,frame,mistake,wrong position\n"
        "2,3,remove,filter,frame,correction,recovered\n",
    )

    captain = raw / "captaincook4d"
    _write(
        captain / "README.md",
        "Official annotations: https://github.com/CaptainCook4D/annotations\n"
        "License: Apache-2.0\n",
    )
    _write(
        captain / "annotation_json" / "1_14.json",
        json.dumps(
            {
                "person_id": 1,
                "steps": [
                    {"step_label": "add ingredient", "error_type": "measurement"}
                ],
            }
        ),
    )
    _write(captain / "videos" / "1_14_360p.mp4")

    unofficial = raw / "assembly101-sample"
    _write(unofficial / "C10095_rgb_low.mp4", b"must not be counted")
    _write(
        unofficial / "labels.json",
        json.dumps({"participant_id": "unofficial", "label": "fake metric label"}),
    )
    return raw


def _dataset(report: dict[str, Any], key: str) -> dict[str, Any]:
    datasets = report["datasets"]
    assert isinstance(datasets, list)
    for dataset in datasets:
        assert isinstance(dataset, dict)
        if dataset.get("key") == key:
            return dataset
    raise AssertionError(f"dataset not found: {key}")


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_audit_discovers_all_supported_datasets_and_counts_metadata(tmp_path: Path) -> None:
    report = audit_datasets(_build_dataset_tree(tmp_path))

    assert report["summary"] == {
        "supported_datasets": 4,
        "present_datasets": 4,
        "local_metric_ready_datasets": 0,
        "inventory_media_files": 3,
        "inventory_annotation_files": 4,
        "metric_eligible_media_files": 0,
        "metric_eligible_annotation_files": 0,
        "excluded_unofficial_assembly101_sample_roots": 1,
        "excluded_unofficial_assembly101_sample_media_files": 1,
    }

    impact = _dataset(report, "impact")
    assert impact["status"] == "quick_start_only"
    assert impact["source"]["verified"] is True
    assert impact["source"]["file_provenance_verified"] is False
    assert impact["inventory"]["participants"] == ["AL07"]
    assert impact["eligible_for_local_metrics"] is False

    industreal = _dataset(report, "industreal")
    assert industreal["status"] == "candidate_requires_dataset_specific_validation"
    assert industreal["eligible_for_local_metrics"] is False
    assert industreal["inventory"]["participants"] == ["01"]
    assert "Install front chassis" in industreal["inventory"]["labels"]

    assembly101 = _dataset(report, "assembly101_mistake")
    assert assembly101["status"] == "annotations_only"
    assert assembly101["inventory"]["participants"] == ["9033"]
    assert {"correct", "mistake", "correction"}.issubset(
        assembly101["inventory"]["labels"]
    )

    captain = _dataset(report, "captaincook4d")
    assert captain["status"] == "candidate_requires_dataset_specific_validation"
    assert captain["eligible_for_local_metrics"] is False
    assert captain["inventory"]["participants"] == ["1"]
    assert "measurement" in captain["inventory"]["labels"]


def test_unofficial_assembly101_sample_is_strictly_excluded(tmp_path: Path) -> None:
    raw = _build_dataset_tree(tmp_path)
    report = audit_datasets(raw)
    exclusion = report["exclusions"][0]

    assert exclusion["path"] == str((raw / "assembly101-sample").resolve())
    assert exclusion["included_in_inventory_totals"] is False
    assert exclusion["included_in_metric_totals"] is False
    assert exclusion["observed_media_files"] == 1
    assert "fake metric label" not in json.dumps(report["datasets"])
    assert report["summary"]["inventory_media_files"] == 3


def test_third_party_api_transfer_is_fail_closed_for_every_dataset(tmp_path: Path) -> None:
    report = audit_datasets(_build_dataset_tree(tmp_path))

    for dataset in report["datasets"]:
        transfer = dataset["third_party_api_transfer"]
        assert transfer["allowed"] is False
        assert transfer["status"].startswith("blocked_")
        assert transfer["reason"]


def test_industreal_tracking_coordinates_are_not_counted_as_labels(tmp_path: Path) -> None:
    raw = _build_dataset_tree(tmp_path)
    tracking = raw / "industreal" / "recordings" / "train" / "01_assy_0_1" / "hands.csv"
    tracking.parent.mkdir(parents=True, exist_ok=True)
    tracking.write_text("000001.jpg,-0.938,123,456\n", encoding="utf-8")

    report = audit_datasets(raw)
    labels = _dataset(report, "industreal")["inventory"]["labels"]

    assert "-0.938" not in labels
    assert "123" not in labels


def test_report_writes_expected_json_and_markdown_without_mutating_data(
    tmp_path: Path,
) -> None:
    raw = _build_dataset_tree(tmp_path)
    before = _snapshot(raw)

    report = audit_datasets(raw)
    json_path, markdown_path = write_reports(report, tmp_path / "reports" / "generated")

    assert _snapshot(raw) == before
    assert json_path.name == "dataset-readiness.json"
    assert markdown_path.name == "dataset-readiness.md"
    persisted = json.loads(json_path.read_text(encoding="utf-8"))
    assert persisted["read_only_scan"] is True
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "# Dataset Readiness Audit" in markdown
    assert "Non-official Assembly101 samples" in markdown
    assert "not a model-accuracy result" in markdown


def test_markdown_escapes_dataset_text() -> None:
    report = {
        "generated_at": "now",
        "scan_root": "/data",
        "summary": {
            "present_datasets": 0,
            "supported_datasets": 0,
            "local_metric_ready_datasets": 0,
            "inventory_media_files": 0,
            "inventory_annotation_files": 0,
        },
        "datasets": [],
        "exclusions": [
            {
                "path": "/data/a|b",
                "reason": "unofficial|sample",
                "observed_media_files": 1,
            }
        ],
    }

    rendered = render_markdown(report)

    assert "/data/a\\|b" in rendered
    assert "unofficial\\|sample" in rendered


def test_cli_accepts_custom_paths_and_writes_both_reports(tmp_path: Path) -> None:
    raw = _build_dataset_tree(tmp_path)
    output = tmp_path / "custom-report"

    result = main(["--data-root", str(raw), "--output-dir", str(output)])

    assert result == 0
    assert (output / "dataset-readiness.json").is_file()
    assert (output / "dataset-readiness.md").is_file()


def test_missing_scan_root_is_reported_without_failure(tmp_path: Path) -> None:
    report = audit_datasets(tmp_path / "missing")

    assert report["summary"]["present_datasets"] == 0
    assert report["summary"]["local_metric_ready_datasets"] == 0
    assert {dataset["status"] for dataset in report["datasets"]} == {"missing"}
    assert report["exclusions"] == []
