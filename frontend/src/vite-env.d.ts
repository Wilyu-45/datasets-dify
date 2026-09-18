/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 基础地址（默认同源 /api，生产可指向独立后端域名） */
  readonly VITE_API_BASE_URL?: string;
  /** 部署子路径（如 /rag/），默认 / */
  readonly VITE_BASE_PATH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}