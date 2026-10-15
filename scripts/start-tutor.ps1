[CmdletBinding()]
param([switch]$SkipInstall)

$ErrorActionPreference = 'Stop'
$tutorRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$tutorBackend = Join-Path $tutorRoot 'backend'
$tutorFrontend = Join-Path $tutorRoot 'frontend'
$tutorPython = Join-Path $tutorBackend '.venv\Scripts\python.exe'
$tutorRuntime = Join-Path $tutorRoot '.runtime\tutor'
$tutorChildren = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$env:PYTHONUTF8 = '1'

function Assert-TutorPortFree([int]$Port) {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $attempt = $probe.ConnectAsync('127.0.0.1', $Port)
        if ($attempt.Wait(400) -and $probe.Connected) {
            throw "端口 $Port 已被占用，请先停止原服务。"
        }
    } catch [System.AggregateException] {
        # A refused connection means the local port is free.
    } finally { $probe.Dispose() }
}

Assert-TutorPortFree 8011
Assert-TutorPortFree 5185

if (!$SkipInstall) {
    if (!(Get-Command uv -ErrorAction SilentlyContinue)) { throw '请先安装 uv。' }
    Push-Location $tutorBackend
    try {
        & uv sync --extra media
        if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
    } finally { Pop-Location }
    if (!(Test-Path -LiteralPath (Join-Path $tutorFrontend 'node_modules'))) {
        Push-Location $tutorFrontend
        try {
            & npm ci --no-audit --no-fund --cache (Join-Path $tutorRoot '.cache\npm')
            if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        } finally { Pop-Location }
    }
}
if (!(Test-Path -LiteralPath $tutorPython)) { throw '缺少 Python 环境，请去掉 -SkipInstall。' }

Push-Location $tutorBackend
try {
    & $tutorPython -m gongxu.tutor_setup
    if ($LASTEXITCODE -ne 0) { throw '初始化失败。' }
} finally { Pop-Location }

New-Item -ItemType Directory -Force -Path $tutorRuntime | Out-Null
try {
    $api = Start-Process -FilePath $tutorPython -ArgumentList @('-m', 'uvicorn', 'gongxu.main:app', '--host', '127.0.0.1', '--port', '8011') -WorkingDirectory $tutorBackend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $tutorRuntime 'api.log') -RedirectStandardError (Join-Path $tutorRuntime 'api-error.log')
    $tutorChildren.Add($api)
    $worker = Start-Process -FilePath $tutorPython -ArgumentList @('-m', 'gongxu.worker') -WorkingDirectory $tutorBackend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $tutorRuntime 'worker.log') -RedirectStandardError (Join-Path $tutorRuntime 'worker-error.log')
    $tutorChildren.Add($worker)
    $node = (Get-Command node -ErrorAction Stop).Source
    $ui = Start-Process -FilePath $node -ArgumentList @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '5185', '--strictPort') -WorkingDirectory $tutorFrontend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $tutorRuntime 'ui.log') -RedirectStandardError (Join-Path $tutorRuntime 'ui-error.log')
    $tutorChildren.Add($ui)
    Write-Host '正在启动。页面：http://127.0.0.1:5185；日志：.runtime/tutor'
    Write-Host '摄像头可在 localhost 使用。退出本窗口或按 Ctrl+C 停止本次服务。'
    while ($true) {
        foreach ($child in $tutorChildren) {
            $child.Refresh()
            if ($child.HasExited) { throw '一个服务已退出，请查看 .runtime/tutor 下的日志。' }
        }
        Start-Sleep -Seconds 1
    }
} finally {
    # Stop only processes created by this invocation; never kill unrelated port owners.
    foreach ($child in $tutorChildren) {
        $child.Refresh()
        if (!$child.HasExited) { $child.Kill() }
        $child.Dispose()
    }
}
