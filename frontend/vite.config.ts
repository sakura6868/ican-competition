import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      registerType: "autoUpdate",
      manifest: {
        name: "工序智传",
        short_name: "工序智传",
        description: "智能技能教育：视频备课、AI 陪练与学习评估",
        theme_color: "#6055d8",
        background_color: "#f6f7fb",
      },
    }),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5185,
    // 仅允许 Cloudflare 临时 HTTPS 隧道回源，避免公开演示时放开任意 Host。
    allowedHosts: [".trycloudflare.com"],
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8011",
        changeOrigin: true,
      },
    },
  },
});
