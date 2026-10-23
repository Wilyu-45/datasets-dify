# Docker 容器化部署指南

本目录提供 **RAG 批量入库系统** 的开箱即用容器化部署方案，将原本「本地直接运行」的部署方式（`run_dev.ps1` / venv + npm）改为一键 Docker 编排。

> ★ 与 `deploy/` 的关系（2026-10 明确）：`deploy/` 是**生产主路线**（MySQL + 官方 MinerU + systemd/脚本与完整 README），
> 本目录 `docker/` 是**轻量单机编排**（自带前端 Nginx 反代、弱网镜像源参数、可选 PostgreSQL）。
> 两者的数据库默认口径已统一为 **MySQL**，后端能力（Playwright Chromium、`--workers 1`）也已对齐；
> 只选一套使用即可，不要同时起两套（镜像名与数据卷名相同，会互相覆盖）。

技术选型（与代码分析对齐）：

- **数据库**：MySQL 8.0（compose 内置，默认）；PostgreSQL 15 为可选兼容模式（`DB_TYPE=postgres` + `--profile postgres`）。
  应用启动时按方言幂等建表，无需手工建库建表
- **前端**：独立 Nginx 容器托管 React 构建产物，并把 `/api`、`/static` 同源反代到后端（无跨域问题）
- **后端**：FastAPI（Python 3.10），镜像内已预装 **Playwright Chromium**（网站抓取反爬 WAF 降级用）
- **未内置** LibreOffice：旧版 `.doc/.xls/.ppt` 在线预览会退回「文件信息 + 下载自查」（如需该能力见文末「可选：启用 LibreOffice」）

---

## 📁 目录文件

| 文件 | 作用 |
| :--- | :--- |
| `docker-compose.yaml` | 编排 `backend` + `frontend` + `mysql`（`postgres` 需 `--profile postgres`） |
| `Dockerfile.backend` | 后端镜像：Python 3.10 多阶段构建 + Playwright Chromium |
| `Dockerfile.frontend` | 前端镜像：Node 构建 React → Nginx 托管 |
| `nginx.conf` | 前端 Nginx 站点配置：SPA 回退 + `/api`、`/static` 反代 |
| `.env.example` | 编排层变量模板（端口 / 数据库账号 / 镜像 tag / 前端构建参数） |

> 构建上下文为**仓库根目录**（compose 中 `context: ..`），所有命令请在仓库根目录执行。

---

## 🚀 快速开始

在**仓库根目录**执行（PowerShell）：

```powershell
# 1. 准备后端业务配置（Dify / OSS / MinerU / 切分策略等 RAG_ 变量）
Copy-Item backend\.env.production.example backend\.env
#    用编辑器按需修改 backend\.env（填入 Dify Key、OSS、MinerU token 等）

# 2. 准备编排层变量（端口 / 数据库账号密码）
Copy-Item docker\.env.example docker\.env
#    用编辑器修改 docker\.env（至少改掉 MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD）

# 3. 构建并后台启动全部服务
docker compose -f docker/docker-compose.yaml up -d --build

# 4. 查看运行状态与日志
docker compose -f docker/docker-compose.yaml ps
docker compose -f docker/docker-compose.yaml logs -f backend
```

启动后访问：

| 入口 | 地址 |
| :--- | :--- |
| **前端页面** | http://localhost:8080 |
| 后端 API 文档（Swagger） | http://localhost:8000/docs |
| 后端健康检查 | http://localhost:8000/api/health |

Linux / macOS 把上面的 `Copy-Item` 换成 `cp` 即可。

---

## ⚙️ 配置说明（两个 .env，职责不同）

容器化部署涉及**两个**配置文件，请勿混淆：

### 1. `backend/.env` —— 业务配置（由 compose `env_file` 注入）

放置所有 `RAG_` 前缀的业务运行时配置：Dify、阿里云 OSS、MinerU、切分策略、存储清理等。可从 `backend/.env.production.example` 复制。

> ★ **机制说明**：后端 `app/config.py` 反转了 pydantic-settings 默认优先级（dotenv > 环境变量），
> 但 `.dockerignore` 排除了 `**/.env`，镜像内**不存在** `backend/.env`，因此 dotenv 源读空、
> 自动回退到「进程环境变量」。compose 通过 `env_file` 把宿主机 `backend/.env` 注入为进程环境变量，从而生效。

**无需在 `backend/.env` 里手工改数据库配置**：compose 的 `environment` 段会覆盖 `env_file`，
把方言与主机指向本编排的服务名（容器内 `127.0.0.1` 指向容器自身，连不上数据库）：

```
RAG_DB_TYPE=${DB_TYPE:-mysql}
RAG_MYSQL_HOST=mysql          # 容器网络下用 compose 服务名
RAG_PG_HOST=postgres          # 仅 DB_TYPE=postgres 兼容模式时读取
RAG_DATA_ROOT=/opt/ragsystem/data
RAG_SERVE_FRONTEND=false      # 前端由独立 Nginx 容器托管
RAG_REQUIRE_ADMIN_FOR_GLOBAL=${REQUIRE_ADMIN_FOR_GLOBAL:-false}
```

即使 `backend/.env` 里写的是别的数据库主机，容器内也以 `environment` 为准。

**切换到 PostgreSQL（兼容模式）**：

```powershell
$env:DB_TYPE = "postgres"
docker compose -f docker/docker-compose.yaml --profile postgres up -d
```

> ⚠ 方言切换**不是无损迁移**：原先写在 `ragsystem_pg_data` 卷里的表数据，切到 MySQL 后
> 应用会在全新的空库上建表（manifest / doc_metadata 需要重新同步）。反向同理。
> 跨方言迁移表数据请走 `mysqldump` / `pg_dump` 或重新跑一次入库流程。

### 2. `docker/.env` —— 编排层变量（compose 自动加载）

compose 会自动加载「compose 文件同目录」的 `.env`（即 `docker/.env`）。仅放编排相关变量：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `TAG` | `latest` | 镜像标签 |
| `FRONTEND_PORT` | `8080` | 前端宿主机端口（浏览器入口） |
| `BACKEND_PORT` | `8000` | 后端宿主机端口（`/docs`、健康检查调试用） |
| `DB_TYPE` | `mysql` | 数据库方言（`mysql` / `postgres`，后者需 `--profile postgres`） |
| `MYSQL_DBNAME` / `MYSQL_USER` | `ragsystem_production` / `ragsystem_app` | 业务库名与应用账号 |
| `MYSQL_PASSWORD` | `ragsystem_secure_password_2026` | **务必修改** |
| `MYSQL_ROOT_PASSWORD` | `ragsystem_root_password_change_me` | **务必修改** |
| `MYSQL_HOST_PORT` | `13306` | MySQL 宿主机映射端口（避开本机已占用的 3306） |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `ragsystem` / `ragsystem` / … | 仅兼容模式使用 |
| `POSTGRES_PORT` | `5432` | 仅在取消 compose 中 postgres 端口映射注释时用到 |
| `RAG_PG_POOL_MIN` / `RAG_PG_POOL_MAX` | `1` / `10` | PG 连接池（仅兼容模式） |
| `REQUIRE_ADMIN_FOR_GLOBAL` | `false` | 全局管理接口是否要求 `X-Admin-Key`（详见 `deploy/README.md`「全局接口访问控制」） |
| `RAG_LOG_LEVEL` | `info` | 后端日志级别 |
| `VITE_API_BASE_URL` | `/api` | 前端 API 基址（默认同源反代，一般无需改） |
| `VITE_BASE_PATH` | `/` | 前端部署子路径 |

> ⚠️ 修改 `MYSQL_*` / `POSTGRES_*` 账号密码后，若数据卷已初始化过，需 `docker compose -f docker/docker-compose.yaml down -v` 删除卷重建才会生效（**会丢失已有数据**，请先备份）。

---

## 🗄️ 数据持久化

compose 定义的命名卷（按方言只用其中一个库卷）：

| 卷名 | 挂载点 | 内容 |
| :--- | :--- | :--- |
| `ragsystem_data` | `/opt/ragsystem/data` | 全部运行时产物：`input/parsed/chunks/output/webscrape/logs` 等 |
| `ragsystem_mysql_data` | `/var/lib/mysql` | MySQL 数据库文件（默认生产路线） |
| `ragsystem_pg_data` | `/var/lib/postgresql/data` | PostgreSQL 数据库文件（仅兼容模式） |

> 业务表：`manifest`（台账）/ `doc_metadata`（元数据）/ `process_config_log`（配置追溯）/ `webscrape_tasks` + `webscrape_records`（抓取台账）。

> `data/input`（源文件）与 `data/parsed`（解析产物）永久保留；`chunks/output/error/webscrape` 由后端存储清理任务按保留期定期清理（见根 `README.md`「生产部署 → 存储清理」）。

### 备份与恢复

```powershell
# 备份数据库（默认 MySQL）
docker exec ragsystem-mysql mysqldump -u ragsystem_app -p$env:MYSQL_PASSWORD ragsystem_production > backup.sql

# 兼容模式（PostgreSQL）备份
docker exec ragsystem-postgres pg_dump -U ragsystem ragsystem > backup.sql

# 备份数据卷
docker run --rm -v ragsystem_data:/data -v ${PWD}:/backup alpine tar czf /backup/data_backup.tar.gz -C /data .

# 恢复数据卷
docker run --rm -v ragsystem_data:/data -v ${PWD}:/backup alpine tar xzf /backup/data_backup.tar.gz -C /data
```

---

## 🛠️ 常用运维命令

> 以下命令均在仓库根目录执行，`-f docker/docker-compose.yaml` 不可省略。

```powershell
# 停止（保留数据卷）
docker compose -f docker/docker-compose.yaml down

# 重启单个服务
docker compose -f docker/docker-compose.yaml restart backend

# 代码更新后重新构建并滚动升级
docker compose -f docker/docker-compose.yaml up -d --build backend
docker compose -f docker/docker-compose.yaml up -d --build frontend

# 无缓存重建（依赖变更时）
docker compose -f docker/docker-compose.yaml build --no-cache backend

# 进入后端容器排查
docker exec -it ragsystem-backend sh

# 完全重置（⚠️ 删除数据卷，会丢数据，请先备份）
docker compose -f docker/docker-compose.yaml down -v
```

---

## 🔌 访问宿主机上的服务（如本地 MinerU）

容器内 `127.0.0.1` 指向容器自身。若 MinerU 部署在宿主机（`RAG_MINERU_PROVIDER=local`），在 `backend/.env` 里把地址指向 `host.docker.internal`（compose 已通过 `extra_hosts` 打通）：

```
RAG_MINERU_PROVIDER=local
RAG_MINERU_API_URL=http://host.docker.internal:7860
```

若使用官方 mineru.net API（`RAG_MINERU_PROVIDER=mineru_net`），则无需本地服务，配好 `RAG_MINERU_NET_TOKEN` 即可。

---

## 🧩 架构与请求链路

```
浏览器 ──> frontend(Nginx:80→宿主8080)
              ├── /            React 静态产物（SPA，history 回退 index.html）
              ├── /api/  ──┐
              └── /static/ ─┴──> backend(FastAPI:8000)
                                        └──> mysql:3306（默认）或 postgres:5432（--profile postgres）—— 业务表
                                        └──> MinerU / Dify / OSS（外部服务）
```

- 前端与 API **同源**，Nginx 统一反代，浏览器无跨域问题，后端不依赖 CORS。
- 后端固定 `--workers 1` 运行：解析进度为进程内存态，多 worker 会导致进度查询失效。

---

## 🩺 故障排查

| 现象 | 排查方向 |
| :--- | :--- |
| **构建时 `apt-get` 报 `502 Bad Gateway`（deb.debian.org）** | 国内网络访问 Debian 官方源受阻。Dockerfile 默认已把 apt/pip/npm/Playwright 换成阿里云 / npmmirror 镜像；若仍失败，检查 `docker/.env` 的 `APT_MIRROR` 等变量，或改用清华源 `APT_MIRROR=mirrors.tuna.tsinghua.edu.cn`（改完 `build --no-cache` 重建） |
| 构建时 pip / npm 超时 | 同上，走国内镜像源；必要时 `docker compose -f docker/docker-compose.yaml build --no-cache` |
| backend 反复重启 | `logs -f backend` 看是否数据库未就绪或 `backend/.env` 配置有误；确认 `mysql`（兼容模式下 `postgres`）已 `healthy` |
| 前端能开但接口 502 | backend 未起来或健康检查未通过；`docker compose ps` 看状态 |
| 建表/连接失败 | 确认 `DB_TYPE` 与已启动的库服务匹配（默认 `mysql`，库卷 `ragsystem_mysql_data`）；检查 `docker/.env` 的 `MYSQL_*` / `POSTGRES_*`；改过密码需 `down -v` 重建卷 |
| 全局接口 401/403 | 确认是否开了 `REQUIRE_ADMIN_FOR_GLOBAL=true`：此时需在反代层注入 `X-Admin-Key`（见 `deploy/README.md`「全局接口访问控制」） |
| 网站抓取遇 WAF 失败 | 确认镜像内 Chromium 已装：`docker exec ragsystem-backend sh -c "ls /ms-playwright"` |
| 上传大文件 413 | Nginx `client_max_body_size` 默认 200m，可在 `nginx.conf` 调大 |
| 端口冲突 | 改 `docker/.env` 的 `FRONTEND_PORT` / `BACKEND_PORT` |

---

## 🔧 可选：启用 LibreOffice（旧版 Office 在线预览）

默认镜像未装 LibreOffice。若需在「网站抓取 / 文件预览」中在线预览旧版 `.doc/.xls/.ppt`，编辑 `Dockerfile.backend`，在 `playwright install` 那一步追加安装：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libreoffice \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
```

并在 `backend/.env` 设置（Linux 容器内 soffice 一般在 PATH，可留空自动探测）：

```
RAG_OFFICE_SOFFICE_PATH=
```

重新构建后端镜像即可（镜像体积会增加约 700MB）。

---

## 🔗 与 `deploy/` 目录的区别

仓库另有一套 `deploy/`（**生产主路线**：MySQL + 官方 mineru.net API + systemd/启停脚本 + 外部 Nginx）。

两套能力已对齐（2026-10）：数据库默认 **MySQL**（都可用 `DB_TYPE=postgres` 切兼容模式）、
后端镜像都装 **Playwright Chromium**、都固定 `--workers 1`、都支持 `REQUIRE_ADMIN_FOR_GLOBAL` 凭据开关。

仍存在的差异：本 `docker/` 自带前端 Nginx 容器与弱网镜像源参数（apt/pip/npm/Playwright），
适合单机一键拉起；`deploy/` 额外提供 nginx 服务、资源限制、`docker-compose.backend-only.yml` 覆盖、
以及 systemd / Windows 启停脚本。两套共用镜像名与数据卷名，**请只选其一**。

---

*最后更新：2026-10-23（数据库默认口径改为 MySQL，与 deploy/ 对齐）*
