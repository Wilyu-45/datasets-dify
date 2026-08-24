# 启动 RAG 后端（开发模式）
# 用法：.\run_dev.ps1 [-Port 8000] [-Host 127.0.0.1]
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$devHost = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = $ScriptDir
$BackendDir = Join-Path $RepoRoot "backend"
$VenvPy = Join-Path $RepoRoot "ragsys\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Error "未找到 venv: $VenvPy`n请先创建：python -m venv ragsys"
    exit 1
}

Write-Host "[run_dev] 启动 FastAPI @ http://${Host}:${Port}" -ForegroundColor Cyan
Write-Host "[run_dev] 后端目录: $BackendDir" -ForegroundColor DarkGray
Write-Host "[run_dev] 日志:    $RepoRoot\data\logs\app.log" -ForegroundColor DarkGray
Write-Host "[run_dev] API 文档:  http://${Host}:${Port}/docs" -ForegroundColor DarkGray
Write-Host "[run_dev] 按 Ctrl+C 停止" -ForegroundColor DarkGray

& $VenvPy -m uvicorn app.main:app `
    --app-dir $BackendDir `
    --host $devHost `
    --port $Port `
    --reload
