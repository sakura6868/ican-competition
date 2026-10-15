"""视频处理测试：probe/transcode/抽帧/场景检测（真实 ffmpeg）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gongxu.media.video import (
    FFmpegNotFoundError,
    extract_keyframes,
    probe,
    scene_boundaries,
    transcode,
)


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> Path:
    """用 ffmpeg 生成 3 秒测试视频。"""
    from gongxu.media.video import _ffmpeg_bin

    try:
        ffmpeg = _ffmpeg_bin()
    except FFmpegNotFoundError:
        pytest.skip("无 ffmpeg，跳过视频处理测试")
    video = tmp_path_factory.mktemp("video") / "sample.mp4"
    proc = __import__("subprocess").run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=320x240:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not video.exists():
        pytest.skip(f"无法生成测试视频: {proc.stderr[-200:]}")
    return video


def test_probe(sample_video: Path) -> None:
    info = probe(sample_video)
    assert info.duration_s >= 2.5
    assert info.width == 320
    assert info.height == 240
    assert info.codec == "h264"


def test_transcode(sample_video: Path, tmp_path: Path) -> None:
    dst = tmp_path / "proxy.mp4"
    transcode(sample_video, dst, width=160)
    assert dst.exists()
    assert dst.stat().st_size > 0
    info = probe(dst)
    assert info.width <= 160


def test_extract_keyframes(sample_video: Path, tmp_path: Path) -> None:
    frames = extract_keyframes(sample_video, tmp_path, fps=2.0, max_frames=10)
    assert len(frames) >= 3
    assert all(f.exists() for f in frames)


def test_scene_boundaries(sample_video: Path) -> None:
    # 静态 testsrc 场景变化少，但函数必须返回列表且不抛错
    boundaries = scene_boundaries(sample_video, threshold=0.4)
    assert isinstance(boundaries, list)
