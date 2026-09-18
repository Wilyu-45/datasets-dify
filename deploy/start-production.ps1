# ═══════════════════════════════════════════
# 生产部署脚本（Windows Server / 开发服务器用）
# 用法：powershell -ExecutionPolicy Bypass -File deploy/start-production.ps1
# ═══════════════════════════════════════════

[CmdletBinding()]
param(
    [int]$Port = 8000,
    # ★ $Host 为 PowerShell 内置只读常量变量，不可用作参数名（会报“无法覆盖变量 Host”）；
    #   改名 $ListenHost 并保留 [Alias("Host")]，兼容 -Host 调用方式。
    [Alias("Host")]
    [string]$ListenHost = "0.0.0.0",
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

# ★ 环境变量读取兼容 Windows PowerShell 5.1（避免使用 PS7 专属的“空合并”语法）：
#   未设置或为空时回退到默认值；脚本已先行加载 backend/.env，此处即为生产配置值。

# 检查 MinerU 通道（local=本地部署 / mineru_net=官方网页端 API / auto=自动降级）
$MineruProvider = $env:RAG_MINERU_PROVIDER; if (-not $MineruProvider) { $MineruProvider = "local" }
if ($MineruProvider -eq "mineru_net") {
    $NetTokenState = if ([string]::IsNullOrWhiteSpace($env:RAG_MINERU_NET_TOKEN)) { "未配置（RAG_MINERU_NET_TOKEN 为空，解析将失败）" } else { "已配置" }
    Write-Host "[OK] MinerU provider: mineru_net（官方网页端 API；Token: $NetTokenState）" -ForegroundColor DarkGray
} else {
    $MineruUrl = $env:RAG_MINERU_API_URL; if (-not $MineruUrl) { $MineruUrl = "http://192.168.31.165:7860" }
    Write-Host "[OK] MinerU provider: $MineruProvider，API: $MineruUrl" -ForegroundColor DarkGray
}

# 检查数据库连接信息（按方言显示：生产 MySQL / 本地开发 PostgreSQL）
$DbType = $env:RAG_DB_TYPE; if (-not $DbType) { $DbType = "postgres" }
if ($DbType -eq "mysql") {
    $MyHost = $env:RAG_MYSQL_HOST; if (-not $MyHost) { $MyHost = "127.0.0.1" }
    $MyPort = $env:RAG_MYSQL_PORT; if (-not $MyPort) { $MyPort = "3306" }
    $MyDbname = $env:RAG_MYSQL_DBNAME; if (-not $MyDbname) { $MyDbname = "ragsystem" }
    Write-Host "[OK] MySQL: ${MyHost}:${MyPort}/${MyDbname}" -ForegroundColor DarkGray
} else {
    $PgHost = $env:RAG_PG_HOST; if (-not $PgHost) { $PgHost = "127.0.0.1" }
    $PgPort = $env:RAG_PG_PORT; if (-not $PgPort) { $PgPort = "5432" }
    $PgDbname = $env:RAG_PG_DBNAME; if (-not $PgDbname) { $PgDbname = "ragsystem" }
    Write-Host "[OK] PostgreSQL: ${PgHost}:${PgPort}/${PgDbname}" -ForegroundColor DarkGray
}

# 检查存储清理（定时任务是否真正启动以运行日志为准）
$CleanupEnabled = $env:RAG_CLEANUP_ENABLED; if (-not $CleanupEnabled) { $CleanupEnabled = "true" }
$CleanupHours = $env:RAG_CLEANUP_SCHEDULE_HOURS; if (-not $CleanupHours) { $CleanupHours = "24" }
Write-Host "[INFO] 存储清理: enabled=$CleanupEnabled, 周期=${CleanupHours}h（0=仅手动触发）" -ForegroundColor DarkGray

# 显示 CORS 配置
$CorsOriginsStr = $env:RAG_CORS_ORIGINS; if (-not $CorsOriginsStr) { $CorsOriginsStr = '["http://localhost:5173","http://localhost:8000"]' }
Write-Host "[INFO] CORS 来源: $CorsOriginsStr" -ForegroundColor DarkGray

Write-Host ""
Write-Host "╔════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║  准备启动 uvicorn...                                  ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# 计算 reload dirs
$BackendDirs = @("$RepoRoot\backend\app")

Write-Host "启动命令:" -ForegroundColor Cyan
# ★ 末尾续行反引号需写成两个（转义后的字面量）：单反引号会转义后引号，
#   造成字符串未闭合级联解析错误（历史遗留 bug，已修复）。
Write-Host "uvicorn app.main:app ``" -ForegroundColor Yellow
Write-Host "  --app-dir $($BackendDirs[0]) ``" -ForegroundColor Yellow
Write-Host "  --host $($ListenHost):$Port ``" -ForegroundColor Yellow
Write-Host "  --workers $($Workers)" -ForegroundColor Yellow
Write-Host ""

# 启动 uvicorn
& $VenvPy -m uvicorn app.main:app `
    --app-dir "$RepoRoot/backend/app" `
    --host $ListenHost `
    --port $Port `
    --workers $Workers `
    --log-level info
