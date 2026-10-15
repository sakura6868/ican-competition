import { expect, test, type Page } from "@playwright/test";

const PASSWORD = "ican-demo-2026";

async function login(page: Page, username: string) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(PASSWORD);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/$/);
}

test("教师能查看真实种子数据生成的班级学情", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await login(page, "teacher");
  await page.getByRole("link", { name: "班级学情" }).click();
  await expect(page.getByRole("heading", { name: "班级学情看板" })).toBeVisible();
  await expect(page.getByText("四步学会平行鞋带穿法", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("演示学员", { exact: true })).toBeVisible();
  await page.screenshot({ path: "../output/playwright/teacher-analytics.png", fullPage: true });
});

test("学生能打开工程回放并查看可解释学习报告", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await login(page, "student");
  await page.getByRole("link", { name: "课程与 AI 陪练" }).click();
  await expect(page.getByText("四步学会平行鞋带穿法", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: /4\/4 · 查看总结/ }).click();
  await expect(page.getByRole("heading", { name: "完成本次课程" })).toBeVisible();
  await expect(page.getByText("本次练习指数")).toBeVisible();
  await expect(page.getByRole("button", { name: /重新练习本课程/ })).toBeVisible();
  await page.screenshot({ path: "../output/playwright/student-report.png", fullPage: true });
});
