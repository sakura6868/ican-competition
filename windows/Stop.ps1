# 工序智传 - Windows 停止（API + worker + Vite）
# Usage: powershell -ExecutionPolicy Bypass -File Stop.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path (Join-Path $RepoRoot ".runtime") "windows-processes.json"

function Stop-GongxuProcessTree {
    param(
        [int]$ProcessId,
        [string]$Name
    )

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Write-Host "[Stop] $Name PID $ProcessId is already stopped" -ForegroundColor DarkGray
        return
    }

    Write-Host "[Stop] stopping $Name process tree (PID $ProcessId)" -ForegroundColor Yellow
    & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0 -and
        $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

$trackedProcesses = @()
if (Test-Path $PidFile) {
    try {
        $manifest = Get-Content -Path $PidFile -Raw | ConvertFrom-Json
        if ($manifest.repo_root -ne $RepoRoot) {
            throw "PID manifest belongs to another repository: $($manifest.repo_root)"
        }
        $trackedProcesses = @($manifest.processes)
    }
    catch {
        throw "Cannot read PID manifest ${PidFile}: $($_.Exception.Message)"
    }
}
else {
    Write-Host "[Stop] PID manifest not found; checking legacy ports 8011 and 5185" -ForegroundColor Yellow
    $connections = Get-NetTCPConnection -LocalPort 8011, 5185 -ErrorAction SilentlyContinue
    $trackedProcesses = @(
        $connections |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object {
                [pscustomobject]@{ name = "legacy-port"; pid = [int]$_ }
            }
    )
}

foreach ($entry in $trackedProcesses) {
    Stop-GongxuProcessTree -ProcessId ([int]$entry.pid) -Name ([string]$entry.name)
}

Start-Sleep -Milliseconds 500
$stillRunning = @(
    $trackedProcesses | Where-Object {
        $null -ne (Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue)
    }
)
if ($stillRunning.Count -gt 0) {
    $remainingIds = ($stillRunning | ForEach-Object { $_.pid }) -join ", "
    throw "Failed to stop process IDs: $remainingIds"
}

Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
Write-Host "[Stop] API, worker, and frontend stopped" -ForegroundColor Green
