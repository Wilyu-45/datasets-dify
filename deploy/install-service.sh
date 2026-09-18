# ═══════════════════════════════════════════
# Linux systemd 服务安装脚本
# 用法：sudo bash deploy/install-service.sh --install|remove
# ═══════════════════════════════════════════

#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

SERVICE_NAME="ragsystem-backend"
SERVICE_USER="${SUDO_USER:-root}"
PYTHON_VENV="$REPO_ROOT/ragsys/bin/python3"
SYSTEMD_SERVICE="/etc/systemd/system/${SERVICE_NAME}.service"

show_help() {
    echo "用法: $0 [--install|--remove]"
    echo "  --install   安装/更新 systemd 服务文件（默认）"
    echo "  --remove    删除 systemd 服务文件"
    exit 1
}

if [ $# -eq 0 ]; then
    set -- --install
fi

case "$1" in
    --install)
        cat > "$SYSTEMD_SERVICE" <<EOF
[Unit]
Description=RAG Batch Ingestion System Backend
# 数据库就绪排序：生产 MySQL（mysql.service）/ 本地开发 PostgreSQL（postgresql.service）；
# After= 为软依赖，不存在的 unit 会被 systemd 忽略，不影响启动。
After=network.target mysql.service postgresql.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$REPO_ROOT
# ★ 后端代码（app/config.py）自动读取 $REPO_ROOT/backend/.env（dotenv 优先于进程环境变量），
#   生产配置请直接编辑 backend/.env，无需 EnvironmentFile（避免优先级混淆）。
# ★ workers 固定为 1：解析进度追踪 parse_progress 为内存态，多 worker 会导致进度查询失效。
ExecStart=$PYTHON_VENV -m uvicorn app.main:app \\
    --app-dir $REPO_ROOT/backend/app \\
    --host 0.0.0.0 \\
    --port 8000 \\
    --workers 1 \\
    --log-level info
Restart=always
RestartSec=10
StandardOutput=append:$REPO_ROOT/data/logs/systemd.log
StandardError=append:$REPO_ROOT/data/logs/systemd.err

# 安全加固
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
        echo "[OK] 已安装 systemd 服务文件: $SYSTEMD_SERVICE"
        echo "[INFO] 执行以下命令启用服务:"
        echo "  sudo systemctl daemon-reload"
        echo "  sudo systemctl enable ${SERVICE_NAME}"
        echo "  sudo systemctl start ${SERVICE_NAME}"
        ;;
    --remove)
        if [ -f "$SYSTEMD_SERVICE" ]; then
            echo "[INFO] 停止并移除服务..."
            sudo systemctl stop ${SERVICE_NAME} || true
            sudo systemctl disable ${SERVICE_NAME} || true
            sudo rm -f "$SYSTEMD_SERVICE"
            sudo systemctl daemon-reload
            echo "[OK] 服务已移除"
        else
            echo "[WARN] 服务文件不存在: $SYSTEMD_SERVICE"
        fi
        ;;
    *)
        show_help
        ;;
esac
