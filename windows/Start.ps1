# 工序智传 - Windows 启动（API + worker + Vite）
# Usage: powershell -ExecutionPolicy Bypass -File Start.ps1 [-NoBrowser]

[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$RuntimeDir = Join-Path $RepoRoot ".runtime"
$LogDir = Join-Path $RuntimeDir "windows-logs"
$PidFile = Join-Path $RuntimeDir "windows-processes.json"
$script:StartedProcesses = @()

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Stop-StartedProcessTree {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Start-GongxuProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory
    )

    $stdoutPath = Join-Path $LogDir "$Name.out.log"
    $stderrPath = Join-Path $LogDir "$Name.err.log"
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru

    $script:StartedProcesses += $process
    Start-Sleep -Milliseconds 500
    $process.Refresh()
    if ($process.HasExited) {
        throw "$Name exited during startup (exit code $($process.ExitCode)); see $stderrPath"
    }

    return [ordered]@{
        name = $Name
        pid = [int]$process.Id
        executable = $FilePath
        working_directory = $WorkingDirectory
        stdout = $stdoutPath
        stderr = $stderrPath
    }
}

function Wait-HttpEndpoint {
    param(
        [string]$Name,
        [string]$Uri,
        [int]$TimeoutSeconds = 45
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "$Name did not become ready within $TimeoutSeconds seconds ($Uri)"
}

if (Test-Path $PidFile) {
    try {
        $existing = Get-Content -Path $PidFile -Raw | ConvertFrom-Json
        $running = @(
            $existing.processes | Where-Object {
                $null -ne (Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue)
            }
        )
        $runningNames = @($running | ForEach-Object { $_.name })
        if ($running.Count -eq 3 -and
            $runningNames -contains "api" -and
            $runningNames -contains "worker" -and
            $runningNames -contains "frontend") {
            Write-Host "[Start] API, worker, and frontend are already running." -ForegroundColor Green
            if (-not $NoBrowser) {
                Start-Process "http://127.0.0.1:5185"
            }
            exit 0
        }
        if ($running.Count -gt 0) {
            Write-Host "[Start] cleaning up an incomplete previous launch" -ForegroundColor Yellow
            & (Join-Path $PSScriptRoot "Stop.ps1")
        }
        else {
            Remove-Item -Path $PidFile -Force
        }
    }
    catch {
        throw "Cannot recover existing PID manifest ${PidFile}: $($_.Exception.Message)"
    }
}

$uvCommand = Get-Command "uv" -ErrorAction Stop
$npmCommand = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command "npm" -ErrorAction Stop
}

Write-Host "[Start] applying database migrations" -ForegroundColor Cyan
Push-Location $BackendDir
try {
    & $uvCommand.Path run python -m gongxu.db.migrate
    if ($LASTEXITCODE -ne 0) {
        throw "database migration failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

# Product route is Qwen-first. Read the same Pydantic settings as the API and
# export the selected backend so API and worker cannot silently disagree.
Push-Location $BackendDir
try {
    $settingsProbe = & $uvCommand.Path run python -c 'from gongxu.config import get_settings; s=get_settings(); print(f"{s.llm_mode}|{int(bool(s.llm_base_url and s.llm_api_key and s.llm_model))}|{s.vision_backend}|{s.llm_model}")'
    if ($LASTEXITCODE -ne 0) {
        throw "cannot read Qwen runtime settings"
    }
}
finally {
    Pop-Location
}
$settingsParts = $settingsProbe.Trim().Split('|')
if ($settingsParts.Count -ne 4) {
    throw "invalid Qwen runtime settings probe"
}
$llmMode = $settingsParts[0]
$llmConfigured = $settingsParts[1] -eq "1"
$configuredVision = $settingsParts[2]
$configuredModel = $settingsParts[3]

if ([string]::IsNullOrWhiteSpace($env:GONGXU_VISION_BACKEND) -and
    $configuredVision -eq "none" -and $llmMode -eq "live" -and $llmConfigured) {
    $env:GONGXU_VISION_BACKEND = "qwen"
}
elseif ([string]::IsNullOrWhiteSpace($env:GONGXU_VISION_BACKEND)) {
    $env:GONGXU_VISION_BACKEND = $configuredVision
}

if ($env:GONGXU_VISION_BACKEND -ne "qwen" -or $llmMode -ne "live" -or -not $llmConfigured) {
    throw "Qwen is the primary model. Configure GONGXU_LLM_MODE=live, Base URL, API Key, MODEL, and GONGXU_VISION_BACKEND=qwen in .env."
}
Write-Host "[Start] Qwen vision backend ready: $configuredModel" -ForegroundColor Green

Write-Host "[Start] launching API :8011, worker, and frontend :5185" -ForegroundColor Cyan

try {
    $api = Start-GongxuProcess `
        -Name "api" `
        -FilePath $uvCommand.Path `
        -ArgumentList @("run", "uvicorn", "gongxu.main:app", "--host", "127.0.0.1", "--port", "8011") `
        -WorkingDirectory $BackendDir
    $worker = Start-GongxuProcess `
        -Name "worker" `
        -FilePath $uvCommand.Path `
        -ArgumentList @("run", "python", "-m", "gongxu.worker") `
        -WorkingDirectory $BackendDir
    $frontend = Start-GongxuProcess `
        -Name "frontend" `
        -FilePath $npmCommand.Path `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5185", "--strictPort") `
        -WorkingDirectory $FrontendDir

    $manifest = [ordered]@{
        schema_version = 1
        repo_root = $RepoRoot
        started_at = (Get-Date).ToUniversalTime().ToString("o")
        processes = @($api, $worker, $frontend)
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $PidFile -Encoding UTF8

    Wait-HttpEndpoint -Name "API" -Uri "http://127.0.0.1:8011/api/v1/health"
    Wait-HttpEndpoint -Name "frontend" -Uri "http://127.0.0.1:5185"
    foreach ($entry in $manifest.processes) {
        if ($null -eq (Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue)) {
            throw "$($entry.name) exited before startup completed; see $($entry.stderr)"
        }
    }

    Write-Host "[Start] ready; PID manifest: $PidFile" -ForegroundColor Green
    Write-Host "[Start] logs: $LogDir" -ForegroundColor DarkGray
    if (-not $NoBrowser) {
        Start-Process "http://127.0.0.1:5185"
    }
}
catch {
    Write-Host "[Start] startup failed; stopping launched process trees" -ForegroundColor Red
    foreach ($startedProcess in $script:StartedProcesses) {
        Stop-StartedProcessTree -Process $startedProcess
    }
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
    throw
}
