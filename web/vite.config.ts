// 开发服务器仅监听本机回环地址，避免未鉴权的控制台被局域网直接访问。
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, host: "127.0.0.1" },
});
