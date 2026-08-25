import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// 前端配置集中在 frontend/.env（复制 frontend/.env.example 后按需修改），
// 这里通过 Vite 的 loadEnv 统一读取，不散落硬编码地址。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    server: {
      port: Number(env.VITE_DEV_PORT || 5173),
      host: "127.0.0.1",
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: true,
    },
  };
});
