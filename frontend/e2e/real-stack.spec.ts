import { expect, test } from "@playwright/test";

const E2E_USERNAME = "e2e_instructor";
const E2E_PASSWORD = "correct-horse-42";

// 0.6 秒、64x64、H.264/yuv420p 的真实 MP4。仅用于验证上传、加密、队列和 FFmpeg worker。
const TINY_MP4_BASE64 =
  "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAANdbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAlgAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAoh0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAlgAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAJYAAAQAAABAAAAAAIAbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAAGABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABq21pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAWtzdGJsAAAAv3N0c2QAAAAAAAAAAQAAAK9hdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABFUxhdmM2MS4xOS4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANWF2Y0MBZAAK/+EAGGdkAAqs2UQmwEQAAAMABAAAAwAoPEiWWAEABmjr48siwP34+AAAAAAQcGFzcAAAAAEAAAABAAAAFGJ0cnQAAAAAAAAnKgAAJyoAAAAYc3R0cwAAAAAAAAABAAAAAwAACAAAAAAUc3RzcwAAAAAAAAABAAAAAQAAAChjdHRzAAAAAAAAAAMAAAABAAAQAAAAAAEAABgAAAAAAQAACAAAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAMAAAABAAAAIHN0c3oAAAAAAAAAAAAAAAMAAALWAAAADgAAAAwAAAAUc3RjbwAAAAAAAAABAAADjQAAAGF1ZHRhAAAAWW1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAALGlsc3QAAAAkqXRvbwAAABxkYXRhAAAAAQAAAABMYXZmNjEuNy4xMDAAAAAIZnJlZQAAAvhtZGF0AAACrQYF//+p3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NCByMzE5MCA3ZWQ3NTNiIC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyNCAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTIgbG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJfcHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWlnaHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTUgc2NlbmVjdXQ9NDAgaW50cmFfcmVmc mVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAhZYiEABH//veIHzLLb5xq13IR56xU/UYjGW1z52a6GKd5AAAACkGaImxD//6pnTQAAAAIAZ5BeQ//BMU=".replaceAll(" ", "");

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(E2E_USERNAME);
  await page.getByLabel("密码").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/$/);
}

test("真实三进程闭环：上传视频由 worker 处理并加载确定性演示报告", async ({ page }) => {
  await login(page);
  const capabilitiesResponse = await page.request.get("/api/v1/system/capabilities");
  expect(capabilitiesResponse.ok()).toBeTruthy();
  const capabilities = await capabilitiesResponse.json() as Array<{
    key: string;
    runtime_backend: string | null;
  }>;
  const assessmentCapability = capabilities.find((item) => item.key === "recorded_assessment");
  test.skip(
    assessmentCapability?.runtime_backend !== "none",
    "真实拒判 smoke 必须在 none 后端运行，禁止测试时调用外部 Provider",
  );

  await page.goto("/knowledge");
  await page.getByLabel("素材类型").selectOption("video");
  await page.getByLabel("授权 / 来源说明").fill("自动化测试生成的非真人色视频");
  await page.locator('input[type="file"]').setInputFiles({
    name: "e2e-real-stack.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from(TINY_MP4_BASE64, "base64"),
  });
  await page.getByRole("button", { name: "上传素材" }).click();

  await expect(page.getByText("e2e-real-stack.mp4", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/资产处理完成/)).toBeVisible({ timeout: 25_000 });
  await expect(page.getByText("视频衍生物已生成").first()).toBeVisible();

  await page.goto("/assessment");
  await expect(
    page.getByText("impact_grinder_safe_subassembly", { exact: true }).first(),
  ).toBeVisible();
  await page.getByTestId("assessment-video").setInputFiles({
    name: "e2e-recorded-assessment.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from(TINY_MP4_BASE64, "base64"),
  });
  const submitted = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/assessment-sessions/submit-video")
      && response.request().method() === "POST",
  );
  await page.getByTestId("start-assessment").click();
  const submission = await (await submitted).json() as { job_id: string };

  await expect(page.getByText("无法判断", { exact: true }).first()).toBeVisible({
    timeout: 25_000,
  });
  await expect(page.getByText("0.0", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/no-vision-adapter/)).toBeVisible();
  const jobResponse = await page.request.get(`/api/v1/jobs/${submission.job_id}`);
  expect(jobResponse.ok()).toBeTruthy();
  const job = await jobResponse.json() as {
    status: string;
    result: { rejected: boolean; reason_code: string; evidence_source: string };
  };
  expect(job.status).toBe("succeeded");
  expect(job.result).toMatchObject({
    rejected: true,
    reason_code: "model_unavailable",
    evidence_source: "vision_rejection",
  });
  await page.getByRole("button", { name: /历史提交/ }).click();
  await expect(
    page.locator(".history-list button").filter({ hasText: "impact_grinder_safe_subassembly" }).first(),
  ).toBeVisible();

  await page.goto("/assessment?demo=complete");
  await expect(page.getByTestId("demo-replay-disclaimer")).toContainText("fixture_replay");
  await expect(page.getByTestId("demo-replay-disclaimer")).toContainText(
    "不代表真实模型准确率",
  );
  await expect(page.getByText("通过", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/· 已确认/).first()).toBeVisible();
});
