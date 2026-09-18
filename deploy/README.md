# RAG 批量入库系统 - 生产部署指南

> 本文档适用于生产环境部署，支持三种模式：**独立部署**、**前后端分离部署**、**子路径部署**。

---

## 📋 目录

- [快速开始](#快速开始)
- [前置依赖](#前置依赖)
- [部署模式选择](#部署模式选择)
- [环境配置](#环境配置)
- [Nginx 配置](#nginx-配置)
- [服务启动与守护](#服务启动与守护)
- [SSL/TLS 证书](#ssltls-证书)
- [数据库初始化](#数据库初始化)
- [备份与恢复](#备份与恢复)
- [故障排查](#故障排查)
- [生产方定制前端](#生产方定制前端)

---

## 🚀 快速开始（5 分钟部署）

以下命令可在 5 分钟内完成 **独立部署**（推荐首次使用）：

```bash
# 1. 克隆代码
cd /opt/ragsystem

# 2. 复制生产配置文件（★ 代码只读 backend/.env，其他文件名不生效）
cp backend/.env.production.example backend/.env
vim backend/.env  # 按【环境配置】章节修改

# 3. 构建前端（可选，独立部署需要）
cd frontend
npm ci --production
npm run build
cd ..

# 4. 安装后端依赖
python3 -m venv ragsys
source ragsys/bin/activate
cd backend
pip install -r requirements.txt
cd ..

# 5. 启动服务
chmod +x deploy/install-service.sh
sudo bash deploy/install-service.sh --install
sudo systemctl daemon-reload
sudo systemctl enable --now ragsystem-backend
sudo systemctl status ragsystem-backend

# 6. 验证访问
curl http://localhost:8000/docs
```

---

## 🐳 Docker 容器化部署（推荐生产环境）

### 快速启动

```bash
# 1. 复制生产配置（docker-compose 会将 backend/.env 注入容器环境变量）
cp backend/.env.production.example backend/.env
vim backend/.env

# 2. 构建并启动所有服务
docker compose -f deploy/docker-compose.yml up -d --build

# 3. 查看日志
docker compose -f deploy/docker-compose.yml logs -f backend

# 4. 健康检查
docker compose -f deploy/docker-compose.yml ps
curl http://localhost:8000/api/health
```

### 架构说明

Docker Compose 自动编排以下服务：

| 服务 | 镜像 | 端口 | 用途 |
|------|------|------|------|
| `backend` | ragsystem-backend:latest | 8000 | FastAPI 后端 |
| `frontend` | ragsystem-frontend:latest | 80 (Nginx) | React 前端 |
| `postgres` | postgres:15-alpine | 5432 | PostgreSQL 数据库 |
| `nginx` | nginx:alpine | 80/443 | 反向代理（可选） |

### 自定义配置

编辑 `deploy/.env.docker` 覆盖默认环境变量：

```bash
cp deploy/.env.docker.example deploy/.env.docker
vim deploy/.env.docker
```

或直接修改 `docker-compose.yml` 中的变量。

### 数据持久化

Docker volumes 自动管理：

```yaml
volumes:
  pg_data:           # PostgreSQL 数据目录
  ragsystem_data:    # 解析文件、chunks 等
  ragsystem_logs:    # 应用日志
```

数据恢复：

```bash
# 备份 PostgreSQL
docker exec ragsystem-postgres pg_dump -U ragsystem_app ragsystem_production > backup.sql

# 备份数据卷
docker run --rm -v ragsystem_data:/data -v $(pwd):/backup \
    alpine tar czf /backup/data_backup.tar.gz -C /data .

# 恢复
docker run --rm -v ragsystem_data:/data -v $(pwd):/backup \
    alpine tar xzf /backup/data_backup.tar.gz -C /data
```

### 扩展部署

**多实例高可用**：

```bash
# 启动 3 个后端实例（负载均衡由 Nginx 自动处理）
docker compose up -d --scale backend=3

# 滚动更新
docker compose up -d --no-deps --build backend
docker compose up -d --no-deps --build frontend
```

**独立前端域名（前后端分离）**：

修改 `docker-compose.yml`，注释掉 `frontend` 服务的 `ports`，由外部 Nginx 代理：

```yaml
# frontend:
#   ports:
#     - "80:80"  # 禁用内部端口映射
```

参考 [nginx-ragsystem.conf](nginx-ragsystem.conf) 模式二配置。

---

## 🔧 详细 Docker 部署指南

### 1. 前置条件

```bash
# 安装 Docker（>=20.10）和 Docker Compose（>=2.20）
docker --version
docker compose version
```

### 2. 生产环境配置

```bash
# 复制后端生产配置（compose 的 env_file 指向此文件）
cp backend/.env.production.example backend/.env
vim backend/.env  # 按实际环境修改

# 可选：Docker 专用环境变量覆盖（compose 自动读取 compose 文件同目录的 .env）
cp deploy/.env.docker.example deploy/.env
vim deploy/.env  # 覆盖数据库密码等变量
```

### 3. 构建镜像

```bash
# 使用 docker-compose 自动构建所有服务
cd /opt/ragsystem
docker compose -f deploy/docker-compose.yml build

# 单独构建某个服务
docker compose -f deploy/docker-compose.yml build backend
docker compose -f deploy/docker-compose.yml build frontend

# 手动构建（更细粒度控制）
docker build -f deploy/Dockerfile.backend -t ragsystem-backend:latest ..
docker build -f deploy/Dockerfile.frontend -t ragsystem-frontend:latest ..
```

### 4. 启动服务

```bash
# 后台启动所有服务
docker compose -f deploy/docker-compose.yml up -d

# 前台启动（查看实时日志）
docker compose -f deploy/docker-compose.yml up

# 指定资源限制（高级用法）
docker compose -f deploy/docker-compose.yml up \
    --memory 4g --cpus 2 backend
```

### 5. 验证部署

```bash
# 检查容器状态
docker compose ps

# 查看后端日志
docker compose logs -f backend

# 查看数据库日志
docker compose logs -f postgres

# 健康检查
curl http://localhost:8000/api/health
wget -qO- http://localhost/

# 测试文件上传
curl -X POST http://localhost:8000/api/upload \
    -F "file=@test.pdf" \
    -H "Authorization: Bearer YOUR_TOKEN"
```

### 6. 日常运维

#### 停止与重启

```bash
# 停止服务（保留数据卷）
docker compose -f deploy/docker-compose.yml down

# 停止并删除容器（保留数据卷）
docker compose -f deploy/docker-compose.yml down --no-rvolumes

# 完全停止（删除数据卷 ⚠️ 危险操作）
docker compose -f deploy/docker-compose.yml down -v

# 重启服务
docker compose -f deploy/docker-compose.yml restart

# 仅重启后端
docker compose -f deploy/docker-compose.yml restart backend
```

#### 更新与回滚

```bash
# 滚动更新后端
docker compose -f deploy/docker-compose.yml up -d --no-deps --build backend

# 拉取最新镜像（如有远程镜像仓库）
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d

# 回滚到上一个版本
docker tag ragsystem-backend:latest ragsystem-backend:$(git rev-parse --short HEAD)
docker compose -f deploy/docker-compose.yml stop backend
docker compose -f deploy/docker-compose.yml run --rm backend docker history ragsystem-backend:v1.2.3
```

#### 清理与重建

```bash
# 清理未使用的镜像和容器
docker system prune -a

# 清理特定服务缓存
docker compose -f deploy/docker-compose.yml build --no-cache backend

# 重置所有数据（⚠️ 备份后再执行）
docker compose -f deploy/docker-compose.yml down -v
docker volume create ragsystem_pg_data
docker volume create ragsystem_data
```

### 7. SSL/TLS 配置

#### 方案一：Let's Encrypt（免费自动续期）

```bash
# 安装 Certbot
docker run -it --rm \
    -v /etc/letsencrypt:/etc/letsencrypt \
    -v /var/lib/letsencrypt:/var/lib/letsencrypt \
    certbot/certbot certonly --standalone -d rag.example.com

# 挂载到 Nginx 容器
# 编辑 docker-compose.yml 的 nginx 服务：
# volumes:
#   - /etc/letsencrypt:/etc/nginx/ssl:ro
```

#### 方案二：自有证书

```bash
# 上传证书到服务器
mkdir -p deploy/ssl
cp your-cert.crt deploy/ssl/fullchain.pem
cp your-key.pem deploy/ssl/privkey.pem
chmod 600 deploy/ssl/privkey.pem

# 在 docker-compose.yml 的 nginx 服务中添加：
# volumes:
#   - ./deploy/ssl:/etc/nginx/ssl:ro
```

### 8. 监控与告警

```bash
# 实时监控资源使用
docker stats ragsystem-backend ragsystem-postgres

# 查看容器元数据
docker inspect ragsystem-backend

# 进入容器调试
docker exec -it ragsystem-backend sh

# 导出 Prometheus 指标（需安装 exporter）
docker run -d \
    --name prometheus-exporter \
    --network=ragsystem_ragsystem-network \
    prom/node-exporter:latest
```

### 9. CI/CD 集成示例（GitHub Actions）

```yaml
# .github/workflows/deploy.yml
name: Deploy to Production
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3
      
      - name: Build and Push
        uses: docker/build-push-action@v5
        with:
          context: .
          file: deploy/Dockerfile.backend
          push: true
          tags: ragsystem-backend:${{ github.sha }}
      
      - name: Deploy to Server
        uses: appleboy/ssh-action@master
        with:
          host: ${{ secrets.SERVER_HOST }}
          username: ${{ secrets.SERVER_USER }}
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd /opt/ragsystem
            docker compose -f deploy/docker-compose.yml pull
            docker compose -f deploy/docker-compose.yml up -d
            docker image prune -f
```


### 服务器要求

| 组件 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | 2 核 | 4 核+ |
| 内存 | 4 GB | 8 GB+ |
| 磁盘 | 50 GB SSD | 100 GB+ SSD |
| OS | Ubuntu 20.04+/CentOS 8+/Windows Server 2019+ | Ubuntu 22.04 LTS |

### 软件依赖

| 软件 | 版本要求 | 用途 |
|------|----------|------|
| Python | 3.10+ | 后端运行时 |
| Node.js | 16.x+ | 前端构建 |
| PostgreSQL | 12+ | 数据库（manifest / doc_metadata） |
| Nginx | 1.18+ | 反向代理（可选） |
| MinerU | 最新稳定版 | PDF/DOCX 解析引擎 |
| Dify | 自托管实例 | 知识库索引与嵌入 |
| 阿里云 OSS | - | 图片永久外链托管 |

### 网络要求

- **出方向**：Dify API (HTTPS:443), MinerU API (内网), OSS API (HTTPS:443)
- **入方向**：HTTP (80), HTTPS (443), SSH (22)

---

## 🎯 部署模式选择

### 模式一：独立部署（推荐中小规模）

**架构**：后端同时服务前端 dist/ + API，Nginx 单点反代

```
Internet → Nginx:80 → 后端 :8000 (前端 + API)
```

**适用场景**：
- 单一团队维护
- 不需要定制前端
- 部署简单，资源占用少

**配置步骤**：

```ini
# backend/.env
RAG_SERVE_FRONTEND=true  # 默认值，挂载前端 dist/
RAG_CORS_ORIGINS=["https://rag.example.com"]
```

**Nginx 配置**：参考 [deploy/nginx-ragsystem.conf](deploy/nginx-ragsystem.conf) 中 **"模式一"** 部分

```bash
# 复制配置
sudo cp deploy/nginx-ragsystem.conf /etc/nginx/sites-available/ragsystem.conf
# 启用并重启
sudo ln -sf /etc/nginx/sites-available/ragsystem.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

### 模式二：前后端分离部署（生产方定制前端）⭐

**架构**：Nginx 分别代理前端静态页面和后端 API

```
Internet → Nginx → 前端 frontend.rag.example.com (静态 HTML/JS)
           ↓
           → 后端 api.rag.example.com (API :8000)
```

**适用场景**：
- 生产方需要定制自己的前端界面
- 前后端由不同团队维护
- 需要独立扩展前端或后端

**配置步骤**：

#### 1. 后端配置（纯 API 服务）

```ini
# backend/.env
RAG_SERVE_FRONTEND=false  # ★ 关键：不挂载前端 dist/
RAG_CORS_ORIGINS=["https://frontend.rag.example.com"]
```

#### 2. 前端构建配置

```env
# frontend/.env.production

# 模式 A：同源部署（Nginx 统一代理到 /api/）
VITE_API_BASE_URL=  # 留空，使用相对路径 /api

# 模式 B：直连独立后端域名
VITE_API_BASE_URL=https://api.rag.example.com/api
```

```bash
# 构建前端
cd frontend
npm ci --production
npm run build
# 产物在 frontend/dist/ 目录
```

#### 3. 部署前端静态文件

```bash
# 将构建产物复制到 Nginx 前端站点目录
sudo mkdir -p /var/www/ragsystem-frontend
sudo cp -r frontend/dist/* /var/www/ragsystem-frontend/
```

#### 4. Nginx 配置

参考 [deploy/nginx-ragsystem.conf](deploy/nginx-ragsystem.conf) 中 **"模式二"** 部分：

```nginx
# 前端服务器
server {
    listen 80;
    server_name frontend.rag.example.com;
    
    root /var/www/ragsystem-frontend;
    index index.html;
    
    location / {
        try_files $uri $uri/ /index.html;
    }
    
    location /api/ {
        proxy_pass http://api.rag.example.com/;
        # ... 代理头设置见完整配置
    }
}

# 后端服务器
server {
    listen 80;
    server_name api.rag.example.com;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        # ... 代理头设置见完整配置
    }
}
```

```bash
# 应用配置
sudo cp deploy/nginx-ragsystem.conf /etc/nginx/sites-available/ragsystem.conf
sudo ln -sf /etc/nginx/sites-available/ragsystem.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

### 模式三：子路径部署（多应用共享域名）

**架构**：RAG 系统部署在 `https://host/rag/` 子路径下

**适用场景**：
- 已有其他应用在同一域名下
- 需要将多个应用放在同一 URL 前缀

**配置步骤**：

```env
# frontend/.env.production
VITE_BASE_PATH=/rag/  # ★ 关键：构建时使用子路径
```

```nginx
# nginx 配置
location /rag/ {
    proxy_pass http://127.0.0.1:8000/;
    # ... 代理头设置
}
```

---

## ⚙️ 环境配置

### 1. 复制并编辑生产配置

> ★★★ **重要**：后端代码（`app/config.py`）硬编码只读取 `backend/.env` 这一个文件，
> 且 dotenv 优先级**高于**进程环境变量。生产配置必须落在 `backend/.env`，
> 其它文件名（如 `.env.production`）不会被读取。改完配置后需重启后端服务。
> 另外：解析进度追踪为内存态，uvicorn 必须固定 `--workers 1`（多 worker 会导致进度查询失效）。

```bash
cd /opt/ragsystem
cp backend/.env.production.example backend/.env
vim backend/.env
```

### 2. 关键配置项说明

#### 🔐 安全敏感项

| 配置项 | 说明 | 示例值 |
|--------|------|--------|
| `RAG_PG_PASSWORD` | PostgreSQL 数据库密码 | 使用强密码生成器 |
| `RAG_OSS_ACCESS_KEY_SECRET` | 阿里云 OSS 密钥 | RAM 子账号密钥 |
| `RAG_DIFY_API_KEY` | Dify 数据集 API Key | `dataset-xxxxx` |
| `RAG_DIFY_APP_API_KEY` | Dify App API Key | `app-xxxxx` |

#### 🌐 网络配置

| 配置项 | 说明 | 必填 |
|--------|------|------|
| `RAG_SERVE_FRONTEND` | 是否挂载前端 dist/（true/false） | ✅ |
| `RAG_CORS_ORIGINS` | 允许的跨域域名列表 | ✅ |
| `RAG_PUBLIC_BASE_URL` | 图片公网访问基础 URL | ✅（OSS 模式） |

#### 🗄️ 数据库配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `RAG_PG_HOST` | 数据库主机 | `127.0.0.1` |
| `RAG_PG_PORT` | 数据库端口 | `5432` |
| `RAG_PG_DBNAME` | 数据库名 | `ragsystem` |
| `RAG_PG_POOL_MAX` | 连接池最大值 | `10`（生产建议 20） |

#### 📄 解析服务配置

| 配置项 | 说明 | 注意 |
|--------|------|------|
| `RAG_MINERU_API_URL` | MinerU 服务地址 | 内网地址即可 |
| `RAG_DIFY_API_URL` | Dify 服务地址 | 必须 HTTPS |
| `RAG_CHUNK_STRATEGY` | 切分策略 | `structure`（默认） |

### 3. 文件权限设置

```bash
# 仅服务运行账号可读（生产环境强制；账号改成实际的服务账号）
sudo chmod 600 backend/.env
sudo chown root:root backend/.env
```

---

## 🌐 Nginx 配置

### 1. 复制配置模板

```bash
sudo cp deploy/nginx-ragsystem.conf /etc/nginx/sites-available/ragsystem.conf
```

### 2. 按需修改

根据实际部署模式修改配置文件中对应的 server block。

### 3. 验证并应用

```bash
sudo nginx -t
sudo systemctl reload nginx
```

### 4. 关键参数说明

| 参数 | 值 | 说明 |
|------|-----|------|
| `client_max_body_size` | `200m` | 支持大 PDF/DOCX 上传 |
| `proxy_read_timeout` | `3600s` | 大文件解析超时（1 小时） |
| `expires` | `30d` | 静态资源缓存 30 天 |
| `add_header X-Frame-Options` | `DENY` | 防止点击劫持 |

### 5. 日志位置

```bash
/var/log/nginx/ragsystem.access.log  # 访问日志
/var/log/nginx/ragsystem.error.log   # 错误日志
```

---

## ▶️ 服务启动与守护

### Linux（systemd）

#### 安装服务

```bash
cd /opt/ragsystem
sudo bash deploy/install-service.sh --install
```

#### 管理服务

```bash
# 启动
sudo systemctl start ragsystem-backend

# 查看状态
sudo systemctl status ragsystem-backend

# 查看日志（实时）
sudo journalctl -u ragsystem-backend -f

# 停止
sudo systemctl stop ragsystem-backend

# 删除服务
sudo bash deploy/install-service.sh --remove
```

#### 自动启动

```bash
sudo systemctl enable ragsystem-backend
```

### Windows（PowerShell 脚本）

```powershell
cd C:\ragsystem
.\deploy\start-production.ps1
```

后台运行（使用 Windows Task Scheduler）：

```powershell
# 创建计划任务
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File C:\ragsystem\deploy\start-production.ps1"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "RAGSystemBackend" -Action $action -Trigger $trigger -User SYSTEM
```

---

## 🔒 SSL/TLS 证书

### 方式一：Let's Encrypt（免费，推荐）

```bash
# 安装 Certbot
sudo apt install certbot python3-certbot-nginx

# 获取证书（需域名 DNS 已解析到服务器）
sudo certbot --nginx -d rag.example.com -d api.rag.example.com

# 自动续期
sudo certbot renew --dry-run
```

### 方式二：自有证书

```bash
# 上传证书到指定位置
sudo mkdir -p /etc/nginx/ssl
sudo cp your-cert.crt /etc/nginx/ssl/ragsystem.crt
sudo cp your-key.key /etc/nginx/ssl/ragsystem.key

# 修改 Nginx 配置
server {
    listen 443 ssl;
    server_name rag.example.com;
    
    ssl_certificate /etc/nginx/ssl/ragsystem.crt;
    ssl_certificate_key /etc/nginx/ssl/ragsystem.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    
    # ... 其他配置
}
```

### 验证证书

```bash
openssl s_client -connect rag.example.com:443 -servername rag.example.com
```

---

## 🗄️ 数据库初始化

### 1. 创建数据库用户

```sql
-- 使用 postgres 超级用户登录
psql -U postgres

-- 创建应用用户（不建议用 postgres 超级用户）
CREATE USER ragsystem_app WITH PASSWORD 'your-strong-password';

-- 创建数据库
CREATE DATABASE ragsystem_production OWNER ragsystem_app;

-- 授予权限
GRANT ALL PRIVILEGES ON DATABASE ragsystem_production TO ragsystem_app;
```

### 2. 自动建表

应用启动时会自动执行幂等建表操作，无需手动创建 `manifest` 和 `doc_metadata` 表。

### 3. 验证连接

```bash
# 测试连接
PGPASSWORD=your-password psql -h 127.0.0.1 -p 5432 -U ragsystem_app -d ragsystem_production -c "SELECT 1;"
```

---

## 💾 备份与恢复

### PostgreSQL 备份

```bash
# 全量备份
pg_dump -U ragsystem_app -h 127.0.0.1 ragsystem_production > backup_ragsystem_$(date +%Y%m%d).sql

# 压缩备份
pg_dump -U ragsystem_app -h 127.0.0.1 ragsystem_production | gzip > backup_ragsystem_$(date +%Y%m%d).sql.gz

# 定时备份（crontab）
0 2 * * * pg_dump -U ragsystem_app ragsystem_production | gzip > /backup/ragsystem_$(date +\%Y\%m\%d).sql.gz
```

### PostgreSQL 恢复

```bash
# 从压缩备份恢复
gunzip < backup_ragsystem_20260918.sql.gz | psql -U ragsystem_app -d ragsystem_production

# 从明文备份恢复
psql -U ragsystem_app -d ragsystem_production < backup_ragsystem_20260918.sql
```

### 数据目录备份

```bash
# 备份整个 data 目录（包含 parsed 文件、chunks 等）
rsync -avz /opt/ragsystem/data/ /backup/ragsystem-data/

# 定时备份建议使用 cron 或 aws s3 sync
```

### 完整灾难恢复

```bash
# 1. 恢复数据库
psql -U ragsystem_app -d ragsystem_production < backup_ragsystem_latest.sql

# 2. 恢复数据文件
rsync -avz /backup/ragsystem-data/ /opt/ragsystem/data/

# 3. 重启服务
sudo systemctl restart ragsystem-backend

# 4. 验证完整性
curl http://localhost:8000/api/health
```

---

## 🔧 故障排查

### 1. 后端无法启动

```bash
# 查看日志
sudo journalctl -u ragsystem-backend -n 100

# 检查端口占用
sudo lsof -i :8000

# 检查 backend/.env 语法
cat backend/.env | grep -v '^#' | grep '='
```

**常见错误**：
- `address already in use` → 端口被占用，杀死旧进程或更换端口
- `database "ragsystem_production" does not exist` → 未创建数据库
- `invalid JSON in RAG_CORS_ORIGINS` → CORS 格式错误，检查引号

### 2. 前端无法访问

```bash
# 检查 Nginx 配置
sudo nginx -t

# 查看 Nginx 错误日志
sudo tail -f /var/log/nginx/ragsystem.error.log

# 确认 dist 目录存在
ls -la frontend/dist/index.html
```

### 3. CORS 跨域失败

```bash
# 检查后端配置
grep RAG_CORS_ORIGINS backend/.env

# 确认请求头中的 Origin 与实际域名匹配
# F12 开发者工具 → Network → 查看 Request Headers
```

### 4. MinerU 解析超时

```bash
# 增加超时时间
# backend/.env
RAG_MINERU_API_TIMEOUT=7200  # 改为 2 小时

# 检查 MinerU 服务状态
curl http://127.0.0.1:7860/health
```

### 5. Dify 索引失败

```bash
# 测试 Dify API 连通性
curl -H "Authorization: Bearer dataset-your-key" \
     https://dify.example.com/v1/datasets

# 检查 dataset ID 是否正确
grep RAG_DIFY_DATASET_ID backend/.env
```

---

## 🎨 生产方定制前端

### 步骤 1：克隆前端代码

```bash
cd /opt/ragsystem
git clone https://your-git-server.com/frontend-custom.git frontend-custom
```

### 步骤 2：修改源码

```bash
cd frontend-custom/src
# 自由修改 React 组件、样式、逻辑...
```

### 步骤 3：构建生产包

```bash
# 设置 API 地址
cat > .env.production <<EOF
VITE_API_BASE_URL=https://api.rag.example.com/api
EOF

# 构建
npm ci --production
npm run build
```

### 步骤 4：部署静态文件

```bash
# 方案 A：Nginx 托管（推荐）
sudo cp -r dist/* /var/www/ragsystem-frontend/

# 方案 B：直接覆盖原 dist
sudo cp -r ../frontend/dist/ /opt/ragsystem/frontend/dist/
# 后端 .env: RAG_SERVE_FRONTEND=true
```

### 步骤 5：更新 Nginx 配置

如果使用独立前端域名，确保 Nginx 的 `root` 指向新的构建目录：

```nginx
root /var/www/ragsystem-frontend-custom;
```

---

## 📊 监控与维护

### 系统监控

```bash
# 磁盘使用
df -h /opt/ragsystem

# 内存使用
free -h

# 进程状态
htop

# 日志实时监控
sudo tail -f /var/log/ragsystem/systemd.log
```

### 健康检查端点

```bash
# 后端健康
curl http://localhost:8000/api/health

# 所有接口列表
curl http://localhost:8000/openapi.json | jq '.paths | keys'
```

### 定期维护清单

| 频率 | 任务 |
|------|------|
| 每日 | 检查日志错误率，监控磁盘空间 |
| 每周 | 清理过期 parsed 文件，备份数据库 |
| 每月 | 更新依赖包（Python/Node），检查安全补丁 |
| 每季度 | 审查访问日志，优化性能瓶颈 |

---

## 📞 技术支持

- **项目仓库**: `/opt/ragsystem`
- **问题反馈**: 查看 `backend/logs/` 和 `data/logs/` 中的详细日志
- **配置文档**: 参考 `backend/.env.example` 和 `frontend/.env.example`
- **API 文档**: 访问 `/docs` (Swagger UI) 或 `/redoc` (ReDoc)

---

## ✅ 部署后验证清单

```
□ 后端能正常启动（systemctl status 显示 active）
□ 前端页面能正常加载（无 404 错误）
□ API 文档页可访问（/docs）
□ 数据库连接成功（查询 manifest 表有数据）
□ CORS 跨域生效（前端能调用后端 API）
□ Nginx 日志正常写入
□ SSL 证书有效（https 访问正常）
□ 文件上传功能正常（上传测试 PDF）
□ MinerU 解析成功（查看解析结果）
□ Dify 索引成功（查看 Dify 控制台）
□ OSS 图片可访问（打开 chunk 中的图片链接）
```

---

*最后更新: 2026-09-18*
