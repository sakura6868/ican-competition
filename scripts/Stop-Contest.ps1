$ErrorActionPreference = 'Stop'
$contestRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pidFile = Join-Path $contestRoot '.runtime\contest\processes.json'
if (!(Test-Path -LiteralPath $pidFile)) { Write-Host '没有发现本项目启动的赛场进程。'; exit 0 }
$owned = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
foreach ($processId in @($owned.api, $owned.worker, $owned.ui)) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) { Stop-Process -Id $processId -Force }
}
Remove-Item -LiteralPath $pidFile -Force
Write-Host '赛场服务已停止。'
