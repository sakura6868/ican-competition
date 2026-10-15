param(
    [string]$Manifest = "data/manifests/qwen-evaluation.json",
    [switch]$PreflightOnly,
    [int]$MaxCases = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$ManifestPath = $Manifest
if (-not [System.IO.Path]::IsPathRooted($ManifestPath)) {
    $ManifestPath = Join-Path $Root $ManifestPath
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。请先运行 windows/Setup.ps1。"
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "评测清单不存在: $ManifestPath"
}

$Arguments = @(
    "run",
    "python",
    "-m",
    "gongxu.evaluation.qwen",
    "--manifest",
    $ManifestPath
)
if ($PreflightOnly) {
    $Arguments += "--preflight-only"
}
if ($MaxCases -gt 0) {
    $Arguments += @("--max-cases", $MaxCases.ToString())
}

Push-Location $Backend
try {
    $env:GONGXU_VISION_BACKEND = "qwen"
    & uv @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
