# ═══════════════════════════════════════════
# 导出 OpenAPI JSON 文档（供第三方/测试工具用）
# 用法：powershell -ExecutionPolicy Bypass -File deploy/export-openapi.ps1 <output_file.json>
# ═══════════════════════════════════════════

[CmdletBinding()]
param(
    [string]$OutputFile = "openapi.json"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$VenvPy = Join-Path $RepoRoot "ragsys\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Host "[ERROR] 未找到虚拟环境: $VenvPy" -ForegroundColor Red
    exit 1
}

Write-Host "正在启动后端以导出 OpenAPI 文档..." -ForegroundColor Cyan

# 启动一个临时 uvicorn 实例并调用 /docs 端点，获取 JSON 格式 OpenAPI schema
try {
    # 使用 uvicorn 的 API 直接获取 schema
    & $VenvPy -c @"
import sys, json, uvicorn
from app.main import app
json.dump(app.openapi(), open("$($OutputFile.Replace('\', '\\\\'))", "w", encoding="utf-8"), indent=2, ensure_ascii=False, default=str)
print("[OK] OpenAPI JSON exported to $($OutputFile)")
"@
} catch {
    Write-Host "[ERROR] 导出失败: $_" -ForegroundColor Red
    exit 1
}
