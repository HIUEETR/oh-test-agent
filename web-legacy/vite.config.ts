// 开发环境默认通过同源 /api 访问后端；代理目标由启动 Vite 时的环境变量注入。
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const configuredTarget = env.VITE_API_PROXY_TARGET || env.API_PROXY_TARGET || env.VITE_API_URL || "http://127.0.0.1:8000";
  const proxyTarget = configuredTarget.replace(/\/api\/?$/, "");

  return {
    plugins: [react()],
    server: {
      port: 5173,
      host: "127.0.0.1",
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
