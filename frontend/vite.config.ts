import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 개발 중에는 /api 요청을 백엔드(thinglens api, 기본 8000)로 넘긴다.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": process.env.THINGLENS_API ?? "http://127.0.0.1:8000" } },
});
