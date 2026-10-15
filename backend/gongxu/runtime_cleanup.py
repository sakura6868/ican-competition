"""Best-effort cleanup for tightly allowlisted plaintext runtime directories.

Only ephemeral request and worker workspaces belong here. Persistent assets,
models, databases, keys, and logs must never be added to these allowlists.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gongxu.config import Settings

logger = logging.getLogger(__name__)

_API_REQUEST_DIRS = ("uploads", "previews", "evidence-previews", "practice-reference")
_WORKER_STARTUP_DIRS = ("work", "sop-compile")


@dataclass
class CleanupReport:
    """Structured result suitable for startup logs and tests."""

    scope: str
    roots_scanned: int = 0
    entries_removed: int = 0
    files_removed: int = 0
    directories_removed: int = 0
    symlinks_removed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


def cleanup_api_request_temps(settings: Settings) -> CleanupReport:
    """Remove plaintext left by interrupted uploads and preview responses."""

    return _cleanup_allowlisted(settings, scope="api_startup", names=_API_REQUEST_DIRS)


def cleanup_worker_startup_temps(settings: Settings) -> CleanupReport:
    """Remove abandoned job workspaces before this worker may claim a job."""

    return _cleanup_allowlisted(settings, scope="worker_startup", names=_WORKER_STARTUP_DIRS)


def _cleanup_allowlisted(
    settings: Settings,
    *,
    scope: str,
    names: tuple[str, ...],
) -> CleanupReport:
    report = CleanupReport(scope=scope)
    runtime_root = settings.data_root / "runtime"
    for name in names:
        _cleanup_root(
            runtime_root / name,
            runtime_root=runtime_root,
            data_root=settings.data_root,
            report=report,
        )
    _log_report(report)
    return report


def _cleanup_root(
    root: Path,
    *,
    runtime_root: Path,
    data_root: Path,
    report: CleanupReport,
) -> None:
    """Clean direct children of one exact runtime root without following links."""

    expected_parent = Path(os.path.abspath(runtime_root))
    absolute_root = Path(os.path.abspath(root))
    if absolute_root.parent != expected_parent:
        _record_error(report, root, "root_not_allowlisted")
        return
    if _has_symlink_component(root, stop=data_root):
        _record_error(report, root, "symlinked_root_rejected")
        return
    if not root.exists():
        return
    if not root.is_dir():
        _record_error(report, root, "root_is_not_directory")
        return

    report.roots_scanned += 1
    try:
        children = list(root.iterdir())
    except OSError as exc:
        _record_error(report, root, f"{type(exc).__name__}: {exc}")
        return

    for child in children:
        if Path(os.path.abspath(child)).parent != absolute_root:
            _record_error(report, child, "child_path_rejected")
            continue
        try:
            _remove_no_follow(child, report)
            report.entries_removed += 1
        except OSError as exc:
            _record_error(report, child, f"{type(exc).__name__}: {exc}")


def _has_symlink_component(path: Path, *, stop: Path) -> bool:
    """Reject a symlink in the runtime path itself; missing components are safe."""

    current = path
    stop_absolute = Path(os.path.abspath(stop))
    while True:
        if current.is_symlink():
            return True
        if Path(os.path.abspath(current)) == stop_absolute:
            return False
        parent = current.parent
        if parent == current:
            return True
        current = parent


def _remove_no_follow(path: Path, report: CleanupReport) -> None:
    """Recursively unlink an entry using lstat so symlinks are never traversed."""

    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        path.unlink()
        report.symlinks_removed += 1
        return
    if not stat.S_ISDIR(mode):
        path.unlink()
        report.files_removed += 1
        return

    with os.scandir(path) as entries:
        for entry in entries:
            _remove_no_follow(Path(entry.path), report)
    path.rmdir()
    report.directories_removed += 1


def _record_error(report: CleanupReport, path: Path, reason: str) -> None:
    report.errors.append({"path": str(path), "reason": reason})


def _log_report(report: CleanupReport) -> None:
    payload = json.dumps(asdict(report), ensure_ascii=True, sort_keys=True)
    if report.errors:
        logger.warning("runtime_plaintext_cleanup %s", payload)
    else:
        logger.info("runtime_plaintext_cleanup %s", payload)
