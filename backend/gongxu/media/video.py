"""媒体处理：基于 FFmpeg 的探测、转码、抽帧、镜头切分。

后端选择：优先系统 ffmpeg/ffprobe，缺失时回退到 imageio-ffmpeg 自带的 ffmpeg
二进制（开发机便捷方案，避免安装 Homebrew）。生产 Windows 使用系统 ffmpeg。
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess  # nosec B404：仅以列表形式执行本地 ffmpeg，无 shell
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class MediaError(Exception):
    pass


class FFmpegNotFoundError(MediaError):
    pass


def _ffmpeg_bin() -> str:
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]  # 第三方库未提供 py.typed，无类型桩

        return cast(str, imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError) as exc:
        raise FFmpegNotFoundError(
            "缺少 ffmpeg；请安装（macOS: brew install ffmpeg）或 uv sync --extra media"
        ) from exc


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    # args 为内部构造的 ffmpeg 参数列表，无 shell，无用户输入拼接
    proc = subprocess.run(  # noqa: S603  # nosec B603
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc


@dataclass
class VideoInfo:
    duration_s: float
    width: int
    height: int
    fps: float
    codec: str


@dataclass(frozen=True)
class SampledFrame:
    """从源视频抽取的一帧，携带全局时间坐标。

    ``source_pts`` 保留 FFmpeg ``showinfo`` 输出的原始 PTS；当运行环境
    无法解析 PTS 时为 ``None``，``timestamp_ms`` 仍由视频时长和全片
    均匀采样位置给出，不会在 batch 之间重置。
    """

    path: Path
    frame_index: int
    timestamp_ms: int
    source_pts: int | None = None


def probe(path: Path) -> VideoInfo:
    """用 `ffmpeg -i` 解析视频元数据（无需 ffprobe）。"""
    ffmpeg = _ffmpeg_bin()
    proc = _run([ffmpeg, "-i", str(path)])
    # ffmpeg -i 正常时 exit 码非 0，元数据在 stderr
    err = proc.stderr or ""
    duration_m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    duration_s = 0.0
    if duration_m:
        h, m, s = duration_m.groups()
        duration_s = int(h) * 3600 + int(m) * 60 + float(s)
    stream_m = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", err)
    codec = stream_m.group(1) if stream_m else ""
    width = int(stream_m.group(2)) if stream_m else 0
    height = int(stream_m.group(3)) if stream_m else 0
    fps_m = re.search(r"(\d+(?:\.\d+)?) fps", err)
    fps = float(fps_m.group(1)) if fps_m else 0.0
    if duration_s <= 0 and width <= 0:
        raise MediaError(f"无法解析视频: {err.strip()[-400:]}")
    return VideoInfo(duration_s=duration_s, width=width, height=height, fps=fps, codec=codec)


def transcode(src: Path, dst: Path, *, width: int = 1280) -> Path:
    """转码为 H.264 MP4（代理视频，供分析与预览）。"""
    ffmpeg = _ffmpeg_bin()
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-vf",
            f"scale={width}:-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-an",
            str(dst),
        ]
    )
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 转码失败: {(proc.stderr or '').strip()[-500:]}")
    return dst


def extract_keyframes(
    src: Path, dst_dir: Path, *, fps: float = 1.0, max_frames: int = 120
) -> list[Path]:
    """按固定帧率抽帧（默认 1fps，最多 max_frames 张）。"""
    ffmpeg = _ffmpeg_bin()
    dst_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(dst_dir / "kf_%04d.jpg")
    proc = _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-vf",
            f"fps={fps}",
            "-frames:v",
            str(max_frames),
            "-q:v",
            "3",
            pattern,
        ]
    )
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 抽帧失败: {(proc.stderr or '').strip()[-500:]}")
    return sorted(dst_dir.glob("kf_*.jpg"))


def extract_frame_at(
    src: Path,
    dst: Path,
    *,
    timestamp_ms: int,
    width: int = 640,
) -> Path:
    """Extract one exact-time JPEG for an authenticated, ephemeral evidence preview."""
    ffmpeg = _ffmpeg_bin()
    dst.parent.mkdir(parents=True, exist_ok=True)
    timestamp_s = max(0, timestamp_ms) / 1000.0
    proc = _run(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{timestamp_s:.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "3",
            str(dst),
        ]
    )
    if proc.returncode != 0 or not dst.is_file():
        raise MediaError(f"ffmpeg 证据帧提取失败: {(proc.stderr or '').strip()[-500:]}")
    return dst


def extract_frames_at(
    src: Path,
    dst_dir: Path,
    *,
    timestamps_ms: list[int],
    width: int = 640,
    frame_index_start: int = 0,
) -> list[SampledFrame]:
    """Extract a sorted, de-duplicated set of exact-time adaptive frames.

    This deliberately builds on :func:`extract_frame_at` so evidence-preview
    extraction and adaptive sampling share the same portable FFmpeg path.  A
    caller can treat the operation atomically: any extraction error is raised,
    while already written files remain confined to ``dst_dir`` for normal job
    cleanup.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    for stale in dst_dir.glob("adaptive_*.jpg"):
        stale.unlink()

    unique_timestamps = sorted({max(0, int(timestamp)) for timestamp in timestamps_ms})
    frames: list[SampledFrame] = []
    for offset, timestamp_ms in enumerate(unique_timestamps):
        path = dst_dir / f"adaptive_{offset:04d}.jpg"
        extract_frame_at(src, path, timestamp_ms=timestamp_ms, width=width)
        frames.append(
            SampledFrame(
                path=path,
                frame_index=frame_index_start + offset,
                timestamp_ms=timestamp_ms,
            )
        )
    return frames


def extract_keyframes_uniform(
    src: Path, dst_dir: Path, *, count: int = 24
) -> list[SampledFrame]:
    """全视频均匀采样，返回携带全局真实时间的帧。

    短于 15 秒的视频会自动降低采样数，避免重复同一源帧。FFmpeg
    ``showinfo`` 用于保留滤镜输出的 PTS/时间；如果某个平台的
    FFmpeg 日志格式不同，则按视频时长计算等价的全局时间坐标。
    """
    if count <= 0:
        return []
    ffmpeg = _ffmpeg_bin()
    dst_dir.mkdir(parents=True, exist_ok=True)
    for stale in dst_dir.glob("kf_*.jpg"):
        stale.unlink()
    pattern = str(dst_dir / "kf_%04d.jpg")
    info = probe(src)
    if info.duration_s <= 0:
        paths = extract_keyframes(src, dst_dir, fps=1.0, max_frames=count)
        return [
            SampledFrame(path=path, frame_index=i, timestamp_ms=i * 1000)
            for i, path in enumerate(paths)
        ]

    effective_count = count
    if info.duration_s < 15:
        # 短片每秒约 1.5 帧，最少覆盖开头和结尾。
        effective_count = min(count, max(2, math.ceil(info.duration_s * 1.5)))

    # count-1 个间隔让首帧接近 0、末帧接近 duration。
    sample_fps = (effective_count - 1) / info.duration_s if effective_count > 1 else 1.0
    endpoint_pad_s = info.duration_s / max(effective_count - 1, 1)
    proc = _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-vf",
            (
                f"tpad=stop_mode=clone:stop_duration={endpoint_pad_s},"
                f"fps={sample_fps}:start_time=0:round=near,showinfo"
            ),
            "-frames:v",
            str(effective_count),
            "-q:v",
            "3",
            pattern,
        ]
    )
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 均匀抽帧失败: {(proc.stderr or '').strip()[-500:]}")

    paths = sorted(dst_dir.glob("kf_*.jpg"))
    pts_rows: list[tuple[int, float]] = []
    showinfo_pattern = re.compile(
        r"\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:\s*(-?[\d.]+)"
    )
    for line in (proc.stderr or "").splitlines():
        match = showinfo_pattern.search(line)
        if match:
            pts_rows.append((int(match.group(1)), float(match.group(2))))

    sampled: list[SampledFrame] = []
    for i, path in enumerate(paths):
        if i < len(pts_rows):
            source_pts, pts_time = pts_rows[i]
            timestamp_ms = max(0, round(pts_time * 1000))
        else:
            source_pts = None
            timestamp_ms = (
                round(i * info.duration_s * 1000 / max(len(paths) - 1, 1))
                if len(paths) > 1
                else 0
            )
        sampled.append(
            SampledFrame(
                path=path,
                frame_index=i,
                timestamp_ms=timestamp_ms,
                source_pts=source_pts,
            )
        )

    # 个别 FFmpeg 版本可能输出重复时间戳；保持单调且不超出时长。
    duration_ms = max(0, round(info.duration_s * 1000))
    normalized: list[SampledFrame] = []
    previous = -1
    for frame in sampled:
        timestamp_ms = min(duration_ms, max(frame.timestamp_ms, previous + 1))
        normalized.append(
            SampledFrame(
                path=frame.path,
                frame_index=frame.frame_index,
                timestamp_ms=timestamp_ms,
                source_pts=frame.source_pts,
            )
        )
        previous = timestamp_ms
    return normalized


def scene_boundaries(src: Path, *, threshold: float = 0.4) -> list[float]:
    """场景检测返回切分点（秒），解析 ffmpeg metadata=print 输出。"""
    ffmpeg = _ffmpeg_bin()
    proc = _run(
        [
            ffmpeg,
            "-i",
            str(src),
            "-vf",
            f"select='gt(scene,{threshold})',metadata=print:file=-",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    # 成功场景检测 exit 0；无输出也视为成功（无切分点）
    boundaries: list[float] = []
    for line in (proc.stdout or "").splitlines():
        match = re.search(r"pts_time:([\d.]+)", line)
        if match:
            boundaries.append(float(match.group(1)))
    if proc.returncode != 0 and not boundaries:
        raise MediaError(f"场景检测失败: {(proc.stderr or '').strip()[-400:]}")
    return sorted(set(boundaries))
