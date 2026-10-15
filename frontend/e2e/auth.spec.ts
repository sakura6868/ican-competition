import { expect, test } from "@playwright/test";

// 可移植 E2E（整改 P1-5）：不依赖预置数据库，只依赖 seed 创建的专用账号。
// 运行前：cd backend && uv run python -m gongxu.db.migrate && uv run python -m gongxu.tests.seed

const E2E_USERNAME = "e2e_instructor";
const E2E_PASSWORD = "correct-horse-42";

test("登录后进入学习总览", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(E2E_USERNAME);
  await page.getByLabel("密码").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: /^你好，/ })).toBeVisible();
});

test("未登录访问受保护页被重定向到登录", async ({ page }) => {
  await page.goto("/sops");
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: "欢迎回到学习空间" })).toBeVisible();
});

test("登录后可导航到各核心页面", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(E2E_USERNAME);
  await page.getByLabel("密码").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/$/);

  // 教学素材页可见上传表单
  await page.getByRole("link", { name: "教学素材" }).click();
  await expect(page.getByRole("heading", { name: "教学素材" })).toBeVisible();
  await expect(page.getByRole("button", { name: "上传素材" })).toBeVisible();
  await expect(page.locator(".nav-item.active")).toHaveCount(1);

  // SOP 课程编排可见（空库也应有编译表单）
  await page.getByRole("link", { name: "SOP 课程编排" }).click();
  await expect(page.getByRole("heading", { name: "SOP 课程编排" })).toBeVisible();
  await expect(page.getByText("标准教学视频", { exact: true })).toBeVisible();

  // 学习评估可见
  await page.getByRole("link", { name: "学习评估" }).click();
  await expect(page.getByRole("heading", { name: "学习评估" })).toBeVisible();

  // 桌面浏览器支持折叠侧栏，重新展开后保留当前页面入口。
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/data-models");
  const toggle = page.getByRole("button", { name: "切换导航菜单" });
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#education-sidebar")).toHaveAttribute("inert", "");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "数据与模型" })).toBeVisible();
  await expect(page.locator(".nav-item.active")).toHaveCount(1);
});

test("已移除的实时训练和工位配置不再出现在产品入口", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(E2E_USERNAME);
  await page.getByLabel("密码").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/$/);

  await expect(page.getByRole("link", { name: "实时训练" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "工位配置" })).toHaveCount(0);

  await page.goto("/live");
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/workstations");
  await expect(page).toHaveURL(/\/$/);
});
