from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = (REPO_ROOT / "windows" / "Start.ps1").read_text(encoding="utf-8")
STOP_SCRIPT = (REPO_ROOT / "windows" / "Stop.ps1").read_text(encoding="utf-8")
SETUP_SCRIPT = (REPO_ROOT / "windows" / "Setup.ps1").read_text(encoding="utf-8")
EVALUATE_SCRIPT = (REPO_ROOT / "windows" / "Evaluate-Qwen.ps1").read_text(encoding="utf-8")
DEV_SCRIPT = (REPO_ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
MIGRATION_BOOTSTRAP = "python -m gongxu.db.migrate"


def test_windows_start_tracks_all_required_processes() -> None:
    assert '"gongxu.main:app"' in START_SCRIPT
    assert '@("run", "python", "-m", "gongxu.worker")' in START_SCRIPT
    assert (
        '@("run", "dev", "--", "--host", "127.0.0.1", "--port", "5185", "--strictPort")'
        in START_SCRIPT
    )
    assert START_SCRIPT.count("Start-GongxuProcess `") == 3
    assert "-PassThru" in START_SCRIPT
    assert '"windows-processes.json"' in START_SCRIPT
    assert "ConvertTo-Json" in START_SCRIPT
    assert "processes = @($api, $worker, $frontend)" in START_SCRIPT


def test_windows_start_is_non_blocking_and_rolls_back_partial_launches() -> None:
    assert not re.search(r"(?m)^\s*npm\s+run\s+dev\s*$", START_SCRIPT)
    assert "Stop-StartedProcessTree" in START_SCRIPT
    assert "startup failed; stopping launched process trees" in START_SCRIPT
    assert 'Wait-HttpEndpoint -Name "API"' in START_SCRIPT
    assert 'Wait-HttpEndpoint -Name "frontend"' in START_SCRIPT


def test_windows_start_migrates_before_launching_services() -> None:
    migration = f"& $uvCommand.Path run {MIGRATION_BOOTSTRAP}"
    launch = 'Write-Host "[Start] launching API :8011, worker, and frontend :5185"'
    assert migration in START_SCRIPT
    assert START_SCRIPT.index(migration) < START_SCRIPT.index(launch)


def test_all_supported_startup_paths_use_compatible_database_bootstrap() -> None:
    assert MIGRATION_BOOTSTRAP in MAKEFILE
    assert MIGRATION_BOOTSTRAP in DEV_SCRIPT
    assert MIGRATION_BOOTSTRAP in SETUP_SCRIPT
    assert MIGRATION_BOOTSTRAP in START_SCRIPT
    assert "alembic upgrade head" not in MAKEFILE
    assert "alembic upgrade head" not in DEV_SCRIPT
    assert "alembic upgrade head" not in SETUP_SCRIPT
    assert "alembic upgrade head" not in START_SCRIPT


def test_windows_stop_uses_manifest_and_stops_process_trees() -> None:
    assert '"windows-processes.json"' in STOP_SCRIPT
    assert "ConvertFrom-Json" in STOP_SCRIPT
    assert "foreach ($entry in $trackedProcesses)" in STOP_SCRIPT
    assert "taskkill.exe /PID $ProcessId /T /F" in STOP_SCRIPT
    assert "Get-NetTCPConnection -LocalPort 8011, 5185" in STOP_SCRIPT
    assert "Remove-Item -Path $PidFile" in STOP_SCRIPT


def test_windows_stop_does_not_overwrite_powershell_pid_constant() -> None:
    assert not re.search(r"(?i)foreach\s*\(\s*\$pid\s+in", STOP_SCRIPT)


def test_windows_qwen_evaluator_uses_same_fail_closed_cli() -> None:
    assert '"gongxu.evaluation.qwen"' in EVALUATE_SCRIPT
    assert '"--manifest"' in EVALUATE_SCRIPT
    assert '"--preflight-only"' in EVALUATE_SCRIPT
    assert '$env:GONGXU_VISION_BACKEND = "qwen"' in EVALUATE_SCRIPT
    assert "GONGXU_LLM_API_KEY" not in EVALUATE_SCRIPT
