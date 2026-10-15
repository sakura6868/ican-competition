"""视频采样元数据测试：不依赖本机 FFmpeg 二进制。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gongxu.media import video


def _fake_ffmpeg_run(
    args: list[str], *, pts_times: list[float]
) -> subprocess.CompletedProcess[str]:
    pattern = Path(args[-1])
    pattern.parent.mkdir(parents=True, exist_ok=True)
    for i, _timestamp in enumerate(pts_times, start=1):
        Path(str(pattern).replace("%04d", f"{i:04d}")).write_bytes(b"jpeg")
    stderr = "\n".join(
        f"[Parsed_showinfo_1] n: {i} pts: {round(ts * 1000)} pts_time:{ts}"
        for i, ts in enumerate(pts_times)
    )
    return subprocess.CompletedProcess(args, 0, stdout="", stderr=stderr)


def test_uniform_sampling_returns_global_pts_across_full_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "input.mp4"
    src.write_bytes(b"video")
    timestamps = [i * 60 / 23 for i in range(24)]
    monkeypatch.setattr(video, "_ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(
        video,
        "probe",
        lambda _path: video.VideoInfo(60.0, 640, 360, 30.0, "h264"),
    )
    monkeypatch.setattr(
        video, "_run", lambda args: _fake_ffmpeg_run(args, pts_times=timestamps)
    )

    frames = video.extract_keyframes_uniform(src, tmp_path / "frames", count=24)

    assert len(frames) == 24
    assert frames[0].timestamp_ms == 0
    assert frames[-1].timestamp_ms == 60_000
    assert all(
        a.timestamp_ms < b.timestamp_ms
        for a, b in zip(frames, frames[1:], strict=False)
    )
    assert [frame.frame_index for frame in frames] == list(range(24))
    assert frames[5].source_pts is not None


def test_short_video_reduces_sample_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "short.mp4"
    src.write_bytes(b"video")
    timestamps = [i * 10 / 14 for i in range(15)]
    monkeypatch.setattr(video, "_ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(
        video,
        "probe",
        lambda _path: video.VideoInfo(10.0, 640, 360, 30.0, "h264"),
    )
    monkeypatch.setattr(
        video, "_run", lambda args: _fake_ffmpeg_run(args, pts_times=timestamps)
    )

    frames = video.extract_keyframes_uniform(src, tmp_path / "frames", count=24)

    assert len(frames) == 15
    assert frames[-1].timestamp_ms == 10_000


def test_extract_frame_at_uses_requested_timestamp_and_bounded_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "input.mp4"
    src.write_bytes(b"video")
    captured: list[str] = []

    def _run_frame(args: list[str]) -> subprocess.CompletedProcess[str]:
        captured.extend(args)
        Path(args[-1]).write_bytes(b"jpeg")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(video, "_ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(video, "_run", _run_frame)

    output = video.extract_frame_at(
        src,
        tmp_path / "evidence.jpg",
        timestamp_ms=4_250,
        width=480,
    )

    assert output.read_bytes() == b"jpeg"
    assert "4.250" in captured
    assert "scale=480:-2" in captured


def test_extract_frames_at_sorts_and_deduplicates_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "input.mp4"
    src.write_bytes(b"video")
    extracted: list[int] = []

    def _extract(
        _src: Path,
        dst: Path,
        *,
        timestamp_ms: int,
        width: int,
    ) -> Path:
        assert width == 480
        extracted.append(timestamp_ms)
        dst.write_bytes(b"jpeg")
        return dst

    monkeypatch.setattr(video, "extract_frame_at", _extract)
    frames = video.extract_frames_at(
        src,
        tmp_path / "adaptive",
        timestamps_ms=[3_000, 1_000, 3_000, -50],
        width=480,
        frame_index_start=24,
    )

    assert extracted == [0, 1_000, 3_000]
    assert [frame.timestamp_ms for frame in frames] == extracted
    assert [frame.frame_index for frame in frames] == [24, 25, 26]
