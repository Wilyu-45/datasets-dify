# 前端

Vite + React 18 + TypeScript + Ant Design 5。

## 安装 Node.js

需要 Node 18+（Vite 5 要求）。推荐安装最新 LTS：
<https://nodejs.org/zh-cn>

## 安装依赖并启动

```powershell
cd d:\programmtools\tools\ragsystem\frontend
npm install
npm run dev
```

开发服务器：<http://127.0.0.1:5173>  
Vite 已将 `/api/*` 代理到 FastAPI 的 `http://127.0.0.1:8000`，所以前端无需 CORS 配置。

## 生产构建

```powershell
npm run build
# 产物在 dist/，FastAPI 在启动时会自动挂载
```

## 启动后端（另一个终端）

```powershell
cd d:\programmtools\tools\ragsystem
.\run_dev.ps1
# 或手动：ragsys\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload
```

后端：<http://127.0.0.1:8000/api/health>  
OpenAPI 文档：<http://127.0.0.1:8000/docs>

## 目录

```
frontend/
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts
└── src/
    ├── main.tsx              # AntD ConfigProvider + zh_CN locale
    ├── App.tsx               # Layout + Menu
    ├── api/client.ts         # fetch 封装
    ├── pages/ScanPage.tsx    # §3.1 主页面
    └── components/
        ├── ScanControl.tsx   # 执行按钮 + 试运行开关 + 统计
        ├── FileTable.tsx     # input/pending 列表
        └── ManifestTable.tsx # manifest 全表（分页）
```
