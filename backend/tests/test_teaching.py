import pytest

from gongxu.providers.base import ProviderError
from gongxu.sop.schema import SopStep
from gongxu.sop.teaching import enrich_steps, plan_windows, sample_teaching_video


@pytest.mark.parametrize("duration", [500, 30_000, 30_001, 65_000, 100_000])
def test_windows_cover_entire_video_without_truncation(duration):
    windows = plan_windows(duration, 30_000)
    assert windows[0][0] == 0 and windows[-1][1] == duration
    assert all(a < b for a, b in windows)
    assert all(windows[i + 1][0] <= windows[i][1] for i in range(len(windows) - 1))


def test_generated_checks_require_teacher_confirmation_and_bind_real_source():
    raw = {
        "steps": [
            {
                "id": "s1",
                "index": 1,
                "name": "安装",
                "weight": 100,
                "teaching": {"start_ms": 100, "end_ms": 900, "instruction": "安装支架"},
                "completion_checks": ["支架在前槽"],
                "observable_checks": [],
                "unobservable_checks": ["连接强度已检查"],
                "verification": {"teacher_confirmed": True},
            }
        ]
    }
    enrich_steps(raw, "real-source", 1000)
    step = SopStep.model_validate(raw["steps"][0])
    assert step.teaching.source_asset_id == "real-source"
    assert not step.verification.teacher_confirmed
    assert step.verification.checks[1].mode == "manual"


@pytest.mark.parametrize(
    "teaching", [None, {"start_ms": 0, "end_ms": 2000}, {"start_ms": 500, "end_ms": 100}]
)
def test_missing_or_invented_ranges_are_rejected(teaching):
    with pytest.raises(ProviderError):
        enrich_steps({"steps": [{"teaching": teaching}]}, "source", 1000)


def test_real_video_sampling_and_coverage(db, video_asset, tmp_path):
    from gongxu.assets.service import decrypt_to_temp

    video = decrypt_to_temp(db, video_asset, temp_dir=tmp_path)
    frames, windows, duration = sample_teaching_video(video, tmp_path / "frames")
    assert len(frames) == 6 and windows == [(0, duration)]
    assert frames[0].timestamp_ms == 0 and frames[-1].timestamp_ms < duration
    assert all(frame.path.is_file() for frame in frames)


def test_twelve_equal_relative_weights_do_not_overweight_the_last_step():
    from gongxu.sop.service import _analysis_to_sop

    result = _analysis_to_sop(
        {
            "steps": [
                {
                    "index": i,
                    "name": f"操作 {i}",
                    "weight": 1,
                    "evidence_refs": [{"kind": "video", "timestamp_ms": 0}],
                }
                for i in range(1, 13)
            ]
        },
        {"reference_timeline": [{"frame_index": 0, "timestamp_ms": 0}]},
        "public_tutorial",
        [],
        reference_video_asset_id="real-source",
        rag_chunks=[],
    )
    weights = [step["weight"] for step in result["steps"]]
    assert sum(weights) == pytest.approx(100)
    assert max(weights) - min(weights) < 0.05


def test_all_windows_reach_compiler_and_publication_requires_teacher(db, video_asset, monkeypatch):
    from copy import deepcopy

    from gongxu.media.video import SampledFrame
    from gongxu.providers.base import ProviderReceipt
    from gongxu.providers.replay import ReplayProvider
    from gongxu.sop import service, teaching

    captured = []
    progress = []

    # Explicit sampling double: real portable extraction is covered by the test above.
    def sample_double(video, directory):
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, stamp in enumerate((0, 600, 1300, 1750)):
            frame = directory / f"frame_{index}.jpg"
            frame.write_bytes(b"sampling test double")
            frames.append(SampledFrame(path=frame, frame_index=index, timestamp_ms=stamp))
        return frames, [(0, 1600), (1300, 2000)], 2000

    class TeachingProviderDouble(ReplayProvider):
        reference_progress = None

        def set_reference_progress_callback(self, callback):
            self.reference_progress = callback

        def analyze_reference_segment(self, segment):
            if self.reference_progress:
                self.reference_progress(1, 1)
            captured.append(segment)
            return {"window": segment.start_ms}, ProviderReceipt(
                provider="teaching-test-double",
                mode="replay",
                model_id="test",
                prompt_version="test",
                input_digest="in",
                response_digest="out",
            )

        def compile_sop(self, analysis, rag_chunks):
            assert len(analysis["window_analyses"]) == 2
            assert analysis["coverage"][-1]["end_ms"] == 2000
            assert "后半段讲解" in captured[-1].asr_text
            steps = []
            for i, stamp in enumerate((0, 1750), 1):
                steps.append(
                    {
                        "index": i,
                        "name": f"清理工作台 {i}",
                        "weight": 50,
                        "predecessors": [1] if i == 2 else [],
                        "tools": ["brush"],
                        "objects": ["workbench"],
                        "actions": ["clean"],
                        "completion_checks": ["工作台表面清洁"],
                        "evidence_refs": [{"kind": "video", "timestamp_ms": stamp}],
                        "teaching": {
                            "start_ms": (i - 1) * 1000,
                            "end_ms": i * 1000,
                            "instruction": "清理并展示工作台表面",
                        },
                    }
                )
            return {"steps": steps}, ProviderReceipt(
                provider="teaching-test-double",
                mode="replay",
                model_id="test",
                prompt_version="test",
                input_digest="in",
                response_digest="out",
            )

    monkeypatch.setattr(teaching, "sample_teaching_video", sample_double)
    monkeypatch.setattr(
        teaching,
        "transcribe",
        lambda *args: ([{"start_ms": 1500, "end_ms": 1900, "text": "后半段讲解"}], "transcribed"),
    )
    row = service.compile_sop_from_media(
        db,
        provider=TeachingProviderDouble(),
        process_key="impact_grinder_safe_subassembly",
        reference_video_asset_id=video_asset,
        manual_asset_ids=[],
        teaching_mode=True,
        progress_callback=lambda stage, percent, message, detail: progress.append(percent),
    )
    assert progress == sorted(progress)
    assert [(s.start_ms, s.end_ms) for s in captured] == [(0, 1600), (1300, 2000)]
    assert row.steps[1]["teaching"]["source_asset_id"] == video_asset
    with pytest.raises(service.SopPublishError, match="教师校对"):
        service.publish_sop(db, row.id)
    updated = deepcopy(row.steps)
    for item in updated:
        item["verification"]["teacher_confirmed"] = True
    row = service.update_sop(db, row.id, steps=updated)
    assert service.publish_sop(db, row.id).status.value == "published"
