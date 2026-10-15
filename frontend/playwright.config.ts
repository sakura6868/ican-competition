import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: "http://localhost:5185",
    trace: "on-first-retry",
  },
  // 前端由 vite dev server 提供（后端 8011 由外部启动，E2E 前置脚本负责）
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5185",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
