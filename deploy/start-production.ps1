# ═══════════════════════════════════════════
# 生产部署脚本（Windows Server / 开发服务器用）
# 用法：powershell -ExecutionPolicy Bypass -File deploy/start-production.ps1
# ═══════════════════════════════════════════

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$Host = "0.0.0.0",
    [int]$Workers = 1,
    [ValidateSet("Production", "Development")]
    [string]$LogEnv = "Production"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Write-Host "" -ForegroundColor Cyan
Write-Host "╔════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║  RAG Batch Ingestion System — Production Startup      ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# 检查虚拟环境
$VenvPy = Join-Path $RepoRoot "ragsys\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "[ERROR] 未找到虚拟环境: $VenvPy" -ForegroundColor Red
    Write-Host "[INFO] 请先执行初始化步骤（见 deploy/README.md）" -ForegroundColor Yellow
    exit 1
}

# ── 生产配置文件检查 ──
# ★ 重要：后端代码（app/config.py）硬编码只读取 backend/.env 这一个文件，
#   且 dotenv 优先级高于进程环境变量。因此生产配置必须落地为 backend/.env，
#   不要用 .env.production 之类其它文件名（代码不会读，改了也不生效）。
#   可用模板：backend/.env.production.example -> 复制为 backend/.env 后填生产值。
$ProdEnv = Join-Path $RepoRoot "backend\.env"
if (Test-Path $ProdEnv) {
    Write-Host "[OK] 已发现生产配置 backend/.env（后端将自动读取，dotenv 优先于环境变量）" -ForegroundColor Green
    # 仅将 backend/.env 载入当前脚本进程环境，用于下方诊断回显；
    # uvicorn 启动后代码仍会以 backend/.env 文件为准（dotenv 优先），故此加载不影响实际配置。
    Get-Content $ProdEnv | Where-Object { $_ -match '^[A-Z_]+=.+$' } | ForEach-Object {
        $key, $value = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($key.Trim(), $value.Trim().Trim('"').Trim("'"))
    }
} else {
    Write-Host "[WARN] 未找到 backend/.env，后端将回退到进程环境变量 / 内置默认值" -ForegroundColor Yellow
    Write-Host "[INFO] 生产部署请先执行: Copy-Item backend/.env.production.example backend/.env 并填写实际值" -ForegroundColor Yellow
}

# 检查 Node.js & 前端构建产物
$NodeExe = Get-Command node -ErrorAction SilentlyContinue
if (-not $NodeExe) {
    Write-Host "[WARN] 未安装 Node.js（如果采用 Nginx 分离部署可忽略此警告）" -ForegroundColor Yellow
} else {
    $FrontendDist = Join-Path $RepoRoot "frontend\dist"
    if (Test-Path $FrontendDist) {
        Write-Host "[OK] 发现前端构建产物 frontend/dist/" -ForegroundColor Green
    } else {
        Write-Host "[WARN] 未发现前端 dist/ 产物（如采用分离部署可忽略；否则请运行 `npm run build`）" -ForegroundColor Yellow
    }
}

# 检查 MinerU 地址
$MineruUrl = $env:RAG_MINERU_API_URL ?? "http://192.168.31.165:7860"
Write-Host "[OK] MinerU API: $MineruUrl" -ForegroundColor DarkGray

# 检查 PostgreSQL 连接信息
$PgHost = $env:RAG_PG_HOST ?? "127.0.0.1"
$PgPort = $env:RAG_PG_PORT ?? "5432"
$PgDbname = $env:RAG_PG_DBNAME ?? "ragsystem"
Write-Host "[OK] PostgreSQL: ${PgHost}:${PgPort}/${PgDbname}" -ForegroundColor DarkGray

# 显示 CORS 配置
$CorsOriginsStr = $env:RAG_CORS_ORIGINS ?? '["http://localhost:5173","http://localhost:8000"]'
Write-Host "[INFO] CORS 来源: $CorsOriginsStr" -ForegroundColor DarkGray

Write-Host ""
Write-Host "╔════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║  准备启动 uvicorn...                                  ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# 计算 reload dirs
$BackendDirs = @("$RepoRoot\backend\app")

Write-Host "启动命令:" -ForegroundColor Cyan
Write-Host "uvicorn app.main:app \`" -ForegroundColor Yellow
Write-Host "  --app-dir $($BackendDirs[0]) \`" -ForegroundColor Yellow
Write-Host "  --host $($Host)`:$Port \`" -ForegroundColor Yellow
Write-Host "  --workers $($Workers)" -ForegroundColor Yellow
Write-Host ""

# 启动 uvicorn
& $VenvPy -m uvicorn app.main:app `
    --app-dir "$RepoRoot/backend/app" `
    --host $Host `
    --port $Port `
    --workers $Workers `
    --log-level info
