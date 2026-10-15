"""Assembly101 intake tests never contact the gated official repository."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.data import assembly101


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "recording_id": (
            "nusar-2021_action_both_9011-a01_9011_user_id_2021-02-01_153724"
        ),
        "views": 1,
        "force": False,
        "accepted_official_terms": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_requires_human_acceptance_before_any_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        assembly101,
        "_download_official_video",
        lambda **kwargs: pytest.fail("must not download before terms acceptance"),
    )
    assert assembly101.cmd_download(_args(accepted_official_terms=False)) == 2


def test_rejects_invalid_recording_identifier() -> None:
    with pytest.raises(assembly101.Assembly101AccessError, match="invalid"):
        assembly101._validate_recording_id("../../private")


def test_cmd_download_registers_official_asset_without_export_by_default(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.db.models import SourceAsset

    root = Path(get_settings().data_root) / "raw" / "assembly101-official"
    source = root / "recordings" / "sample" / "C10095_rgb.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"official-video")
    monkeypatch.setattr(assembly101, "_download_official_video", lambda **kwargs: source)

    assert assembly101.cmd_download(_args()) == 0
    row = (
        db.query(SourceAsset)
        .filter(SourceAsset.filename.like("Assembly101_%_C10095.mp4"))
        .first()
    )
    assert row is not None
    assert row.allow_provider_export is False
    assert "official" in row.license_note
    assert "CC BY-NC" in row.license_note


def test_download_argument_cannot_enable_provider_export(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.db.models import SourceAsset

    source = Path(get_settings().data_root) / "raw" / "assembly101-official" / "video.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"official-video-export")
    monkeypatch.setattr(assembly101, "_download_official_video", lambda **kwargs: source)

    assert assembly101.cmd_download(_args(allow_provider_export=True)) == 0
    row = (
        db.query(SourceAsset)
        .filter(SourceAsset.filename.like("Assembly101_%_C10095.mp4"))
        .first()
    )
    assert row is not None
    assert row.allow_provider_export is False


def test_cli_rejects_provider_export_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gongxu.data.assembly101",
            "--recording-id",
            "nusar-2021_action_both_9011-a01_9011_user_id_2021-02-01_153724",
            "--accepted-official-terms",
            "--allow-provider-export",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        assembly101.main()
    assert exc_info.value.code == 2


def test_access_failure_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        assembly101,
        "_download_official_video",
        lambda **kwargs: (_ for _ in ()).throw(
            assembly101.Assembly101AccessError("gated")
        ),
    )
    assert assembly101.cmd_download(_args()) == 1


def test_robot_sop_aligns_observations() -> None:
    from gongxu.sop.fixtures import robot_assemble_sop
    from gongxu.vision import align, facts

    sop = robot_assemble_sop()
    observations = [
        facts.Observation(
            timestamp_ms=8000,
            hand="both",
            tool="",
            object="tracked_robot",
            action="hold",
            relation="both_operates_tracked_robot",
            confidence=0.85,
            frame_hash="a",
        ),
        facts.Observation(
            timestamp_ms=14000,
            hand="right",
            tool="screwdriver",
            object="tracked_robot",
            action="adjust",
            relation="right_holds_screwdriver",
            confidence=0.7,
            frame_hash="b",
        ),
    ]
    alignment = align.align_observations(sop.steps, observations)
    by_index = {result.step.index: result.judgement for result in alignment.step_results}
    assert by_index[2] == "passed"
    assert by_index[5] == "passed"
    assert by_index[1] == "insufficient_evidence"
