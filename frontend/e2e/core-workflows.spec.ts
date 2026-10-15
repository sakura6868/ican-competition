import { expect, test, type Page, type Route } from "@playwright/test";

const step = {
  id: "step-1", index: 1, name: "确认工件", predecessors: [], optional: false,
  critical: true, tools: ["扳手"], objects: ["工件"], actions: ["确认"],
  expected_duration_s: 20, observable_checks: ["工件进入画面"], unobservable_checks: [],
  weight: 100, evidence_refs: [{ kind: "video", ref: "00:00-00:04", description: "标准视频", asset_id: "video-1" }],
};

const draftSop = {
  id: "sop-draft", process_key: "fixture_process", version: 1, status: "draft",
  source_grade: "video_reference", steps: [step],
  scoring_profile: { completeness: .3, order: .2, tools_parts: .15, action_quality: .2, efficiency: .05, observable_safety: .1 },
  prompt_version: "fixture", provider_receipt_id: null, reference_asset_id: "video-1",
  source_asset_ids: ["video-1"], created_at: "2026-08-24T00:00:00Z",
};

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockIdentity(page: Page, role: "admin" | "instructor" | "learner" | "observer") {
  await page.route("**/api/v1/auth/me", (route) => fulfillJson(route, { id: `${role}-1`, username: role, display_name: role, role }));
  await page.route("**/api/v1/auth/csrf", async (route) => {
    await route.fulfill({ status: 204, headers: { "set-cookie": "gx_csrf=e2e; Path=/" } });
  });
}

test("教师从知识库上传并处理视频后异步生成 SOP 草稿", async ({ page }) => {
  await mockIdentity(page, "instructor");
  const videoAsset = {
    id: "asset-standard-video", kind: "video", filename: "标准装配教学.mp4",
    sha256: "a".repeat(64), license_note: "校内实训授权", authorized_by: "instructor-1",
    has_encrypted_storage: true, allow_provider_export: true, created_at: "2026-08-24T00:00:00Z",
  };
  const generatedSop = {
    ...draftSop, id: "sop-from-video", process_key: "fixture_installation",
    reference_asset_id: videoAsset.id, source_asset_ids: [videoAsset.id],
  };
  let assets: typeof videoAsset[] = [];
  let sops: typeof generatedSop[] = [];
  let compilePayload: Record<string, unknown> | null = null;

  await page.route("**/api/v1/assets", async (route) => {
    if (route.request().method() === "GET") return fulfillJson(route, assets);
    return route.fallback();
  });
  await page.route("**/api/v1/assets/upload-and-process", async (route) => {
    assets = [videoAsset];
    return fulfillJson(route, {
      asset: videoAsset, job_id: "asset-job-1", job_status: "queued",
    }, 201);
  });
  await page.route("**/api/v1/jobs/asset-job-1", async (route) => {
    await fulfillJson(route, {
      id: "asset-job-1", job_type: "asset_process", status: "succeeded", stage: "done",
      progress_percent: 100, status_text: "视频处理完成", error_summary: "", retry_count: 0,
      max_retries: 3, result: {}, checkpoint: {}, created_at: "",
      started_at: null, finished_at: null,
    });
  });
  await page.route("**/api/v1/sops", (route) => fulfillJson(route, sops));
  await page.route("**/api/v1/sops/compile-jobs", async (route) => {
    compilePayload = route.request().postDataJSON();
    await fulfillJson(route, { job_id: "compile-job-1", status: "queued" }, 202);
  });
  await page.route("**/api/v1/jobs/compile-job-1", async (route) => {
    sops = [generatedSop];
    await fulfillJson(route, {
      id: "compile-job-1", job_type: "sop_compile", status: "succeeded", stage: "completed",
      progress_percent: 100, status_text: "草稿编译完成", error_summary: "", retry_count: 0,
      max_retries: 3, result: { sop_id: generatedSop.id }, checkpoint: {}, created_at: "",
      started_at: null, finished_at: null,
    });
  });

  await page.goto("/knowledge");
  await page.getByLabel("素材类型").selectOption("video");
  await page.getByLabel("授权 / 来源说明").fill("校内实训授权");
  await page.getByText(/允许将本资产的关键帧/).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: videoAsset.filename, mimeType: "video/mp4", buffer: Buffer.from("offline-video-fixture"),
  });
  await page.getByRole("button", { name: "上传素材" }).click();
  await expect(page.getByText(videoAsset.filename, { exact: true })).toBeVisible();
  await expect(page.getByText(/资产处理完成/)).toBeVisible();

  await page.getByRole("link", { name: "编排课程" }).click();
  await expect(page).toHaveURL(new RegExp(`/sops\\?asset=${videoAsset.id}`));
  await expect(page.getByRole("heading", { name: "SOP 课程编排" })).toBeVisible();
  await expect(page.getByText("已从知识库带入标准教学视频，可以直接生成 SOP 草稿。")).toBeVisible();
  await expect(page.getByLabel("标准教学视频")).toHaveValue(videoAsset.id);
  await page.getByTestId("process-key").fill("fixture_installation");
  await page.getByRole("button", { name: "生成 SOP 草稿" }).click();

  await expect(page.getByText("SOP 草稿已生成，可在下方校对步骤后保存并发布。")).toBeVisible();
  await expect(page.getByRole("heading", { name: /fixture_installation/ })).toBeVisible();
  expect(compilePayload).toMatchObject({
    process_key: "fixture_installation", reference_video_asset_id: videoAsset.id,
  });
});

test("教师编辑、保存并发布 SOP", async ({ page }) => {
  await mockIdentity(page, "instructor");
  let sop = structuredClone(draftSop);
  await page.route("**/api/v1/assets", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/sops/vocabularies/fixture_process", (route) => fulfillJson(route, {
    process_key: "fixture_process", tools: ["扳手"], objects: ["工件"], actions: ["确认"],
    approved: true, source: "teacher_confirmed",
  }));
  await page.route("**/api/v1/sops", async (route) => {
    if (route.request().method() === "GET") return fulfillJson(route, [sop]);
    return route.fallback();
  });
  await page.route("**/api/v1/sops/sop-draft", async (route) => {
    const payload = route.request().postDataJSON();
    sop = { ...sop, steps: payload.steps };
    await fulfillJson(route, sop);
  });
  await page.route("**/api/v1/sops/sop-draft/publish", async (route) => {
    sop = { ...sop, status: "published" };
    await fulfillJson(route, sop);
  });

  await page.goto("/sops");
  await expect(page.getByRole("heading", { name: "SOP 课程编排" })).toBeVisible();
  await page.getByLabel("步骤名称").fill("确认工件与夹具");
  await page.getByRole("button", { name: "保存草稿" }).click();
  await expect(page.getByText("草稿已保存。" )).toBeVisible();
  await page.getByRole("button", { name: "发布版本" }).click();
  await expect(page.getByText(/已发布，学员现在可以选择/)).toBeVisible();
});

test("学员上传视频后自动进入证据报告", async ({ page }) => {
  await mockIdentity(page, "learner");
  const published = { ...draftSop, id: "sop-published", status: "published" };
  const session = {
    id: "session-1", mode: "recorded", process_key: published.process_key, status: "running",
    learner_id: "learner-1", input_sha256: "abc", input_asset_id: "asset-1",
    sop_version_id: published.id, job_id: "job-1", progress_percent: 25,
    progress_stage: "sampling", created_at: "2026-08-24T00:00:00Z",
  };
  const job = {
    id: "job-1", job_type: "recorded_assessment", status: "running", stage: "vision_analyzing",
    progress_percent: 55, status_text: "正在识别动作", error_summary: "", retry_count: 0,
    max_retries: 3, result: {}, checkpoint: {}, created_at: "", started_at: null, finished_at: null,
  };
  let progressReads = 0;
  await page.route("**/api/v1/sops", (route) => fulfillJson(route, [published]));
  await page.route("**/api/v1/assets", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/assessment-sessions", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/assessment-sessions/submit-video", (route) => fulfillJson(route, { session, job_id: "job-1", job_status: "queued", preview_url: null }, 201));
  await page.route("**/api/v1/assessment-sessions/session-1/progress", (route) => {
    progressReads += 1;
    const complete = progressReads > 1;
    return fulfillJson(route, {
      session_id: "session-1", session_status: complete ? "completed" : "running",
      job: { ...job, status: complete ? "succeeded" : "running", progress_percent: complete ? 100 : 55 },
      report_ready: complete, report_id: complete ? "report-1" : null,
      analyzed_frames: complete ? 24 : 12, estimated_remaining_seconds: complete ? 0 : 4,
    });
  });
  await page.route("**/api/v1/assessment-sessions/session-1/report", (route) => fulfillJson(route, {
    report: { id: "report-1", session_id: "session-1", total_score: 42, result: "inconclusive", dimension_scores: { completeness: 50, order: 40 }, evidence_coverage: .55, model_version: "replay", scoring_rules_version: "scoring-v3" },
    steps: [{ step_index: 1, step_id: "step-1", judgement: "insufficient_evidence", start_ms: 4200, end_ms: 6600, rule_code: "EVIDENCE_LOW", reason: "手部被工件遮挡", observable_check_results: { "工件进入画面": "insufficient_evidence" } }],
    coaching: {
      version: "coaching-v1", source: "external_model", explanation: "先调整机位，再按标准步骤复练。",
      practice_plan: { focus_steps: ["step-1"], repeat_count: 3, note: "连续完成三次后重新考评。" },
      provider: "qwen38max", model_id: "fixture-model", errors: [],
    },
    analysis_duration_ms: 12_300,
  }));
  await page.route("**/api/v1/assessment-sessions/session-1/evidence", (route) => fulfillJson(route, {
    session_id: "session-1", input_asset_id: "asset-1", preview_url: null,
    steps: [{ step_index: 1, step_id: "step-1", judgement: "insufficient_evidence", start_ms: 4200, end_ms: 6600, rule_code: "EVIDENCE_LOW", reason: "手部被工件遮挡", confidence: .42, keyframe_ref: null, observable_check_results: { "工件进入画面": "insufficient_evidence" } }],
  }));

  await page.goto("/assessment");
  await page.getByTestId("assessment-video").setInputFiles({ name: "learner-demo.mp4", mimeType: "video/mp4", buffer: Buffer.from("fixture") });
  await expect(page.getByTestId("start-assessment")).toBeDisabled();
  await page.getByTestId("provider-export-consent").check();
  await page.getByTestId("start-assessment").click();
  await expect(page.getByText("无法判断", { exact: true }).first()).toBeVisible({ timeout: 5_000 });
  await expect(page.getByText("手部被工件遮挡").first()).toBeVisible();
  await expect(page.getByText("工件进入画面 · 证据不足")).toBeVisible();
  await expect(page.getByText(/仅供参考，不判定为通过/)).toBeVisible();
  await expect(page.getByText("先调整机位，再按标准步骤复练。")).toBeVisible();
  await expect(page.getByText("12.3s")).toBeVisible();
  await expect(page.getByText(/大模型只解释既有判定，不参与改分/)).toBeVisible();
});

test("报告失败步骤会选中证据并把播放器定位到对应时间", async ({ page }) => {
  await mockIdentity(page, "learner");
  await page.addInitScript(() => {
    // Chromium cannot seek an intentionally tiny offline media fixture. The shim records
    // the real EvidencePlayer assignment while keeping the test fully local and deterministic.
    Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
      configurable: true,
      get() { return Number((this as HTMLMediaElement & { __e2eCurrentTime?: number }).__e2eCurrentTime ?? 0); },
      set(value: number) {
        (this as HTMLMediaElement & { __e2eCurrentTime?: number }).__e2eCurrentTime = Number(value);
      },
    });
    HTMLMediaElement.prototype.play = () => Promise.resolve();
  });
  await page.route("**/api/v1/sops", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/assets", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/assessment-sessions", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/demo/load/error", (route) => fulfillJson(route, {
    scenario: "error", label: "明确错误", source: "fixture_replay", cached: true,
    disclaimer: "离线工程回放，不代表真实模型准确率。",
    sop: { id: "fixture-sop", process_key: "fixture_process", version: 1 },
    report: {
      id: "report-fail", session_id: "session-fail", total_score: 38, result: "fail",
      dimension_scores: { completeness: 45, order: 30 }, evidence_coverage: 1,
      model_version: "fixture-replay-v1", scoring_rules_version: "scoring-v2",
    },
    steps: [
      { step_index: 1, step_id: "step-1", step_name: "确认工件", judgement: "passed", start_ms: 0, end_ms: 1800, rule_code: "ok", reason: "工件已确认" },
      { step_index: 2, step_id: "step-2", step_name: "拧紧夹具", judgement: "failed", start_ms: 4200, end_ms: 6200, rule_code: "WRONG_TOOL", reason: "错用了钳子" },
    ],
    observations: [], quality: { sampled_frames: 24, visible_ratio: 1 },
    preview_url: "data:video/mp4;base64,AAAAIGZ0eXBpc29t",
  }));

  await page.goto("/assessment?demo=error");
  const video = page.getByRole("region", { name: "步骤证据播放器" }).locator("video");
  await expect(video).toBeVisible();
  await page.locator(".judgement-row").filter({ hasText: "错用了钳子" }).click();

  const selectedSegment = page.getByRole("button", { name: "跳转到步骤 2，未通过" });
  await expect(selectedSegment).toHaveClass(/active/);
  await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).currentTime)).toBe(3.2);
});

test("手机端考评向导无横向溢出且主操作满足触控尺寸", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockIdentity(page, "learner");
  const published = { ...draftSop, id: "sop-published", status: "published" };
  await page.route("**/api/v1/sops", (route) => fulfillJson(route, [published]));
  await page.route("**/api/v1/assets", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/assessment-sessions", (route) => fulfillJson(route, []));

  await page.goto("/assessment");
  await expect(page.getByRole("link", { name: "教学素材" })).toHaveCount(0);
  await page.getByTestId("assessment-video").setInputFiles({ name: "stale.mp4", mimeType: "video/mp4", buffer: Buffer.from("fixture") });
  await page.getByRole("button", { name: "使用已有视频" }).click();
  await expect(page.getByTestId("start-assessment")).toBeDisabled();
  const metrics = await page.evaluate(() => ({ width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.width);
  const buttonBox = await page.getByTestId("start-assessment").boundingBox();
  expect(buttonBox?.height).toBeGreaterThanOrEqual(44);
});

test("工作台演示入口无需上传即可加载带免责声明的工程回放", async ({ page }) => {
  await mockIdentity(page, "learner");
  await page.route("**/api/v1/sops", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/assets", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/assessment-sessions", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/demo/load/complete", (route) => fulfillJson(route, {
    scenario: "complete",
    label: "完整操作",
    source: "fixture_replay",
    cached: true,
    disclaimer: "离线工程回放，用于演示产品行为，不代表真实模型准确率或工业认证结果。",
    sop: { id: "fixture-sop", process_key: "fixture_process", version: 1 },
    report: { id: "demo-report", session_id: "demo-session", total_score: 96, result: "pass", dimension_scores: { completeness: 100 }, evidence_coverage: 1, model_version: "fixture-replay-v1", scoring_rules_version: "scoring-v3" },
    steps: [{ step_index: 1, step_id: "step-1", step_name: "确认工件", judgement: "passed", start_ms: 0, end_ms: 2000, rule_code: "ok", reason: "事件顺序与 SOP 一致", observable_check_results: { "工件进入画面": "passed" } }],
    observations: [],
    quality: { sampled_frames: 24, visible_ratio: 1 },
    preview_url: null,
  }));

  await page.goto("/assessment?demo=complete");
  await expect(page.getByTestId("demo-replay-disclaimer")).toContainText("fixture_replay");
  await expect(page.getByTestId("demo-replay-disclaimer")).toContainText("不代表真实模型准确率");
  await expect(page.getByText("通过", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("工件进入画面 · 已确认")).toBeVisible();
  await expect(page.getByTestId("assessment-video")).toHaveCount(0);
});

test("管理员创建账号后可在账号列表中停用", async ({ page }) => {
  await mockIdentity(page, "admin");
  type AdminUser = { id: string; username: string; display_name: string; role: string; is_active: boolean };
  let users: AdminUser[] = [{ id: "admin-1", username: "admin", display_name: "管理员", role: "admin", is_active: true }];
  let createdPayload: Record<string, unknown> | null = null;

  await page.route("**/api/v1/admin/users", async (route) => {
    if (route.request().method() === "GET") return fulfillJson(route, users);
    createdPayload = route.request().postDataJSON();
    const created: AdminUser = {
      id: "learner-new", username: String(createdPayload?.username),
      display_name: String(createdPayload?.display_name), role: String(createdPayload?.role), is_active: true,
    };
    users = [...users, created];
    await fulfillJson(route, created, 201);
  });
  await page.route("**/api/v1/admin/users/learner-new", async (route) => {
    const update = route.request().postDataJSON() as { is_active: boolean };
    users = users.map((user) => user.id === "learner-new" ? { ...user, is_active: update.is_active } : user);
    await fulfillJson(route, users.find((user) => user.id === "learner-new"));
  });

  await page.goto("/admin-users");
  await page.getByLabel("用户名").fill("student_2026");
  await page.getByLabel("密码").fill("Training#2026");
  await page.getByLabel("显示名").fill("实训学员");
  await page.getByLabel("角色").selectOption("learner");
  await page.getByRole("button", { name: "创建" }).click();

  await expect(page.getByText("已创建账号 student_2026")).toBeVisible();
  const learnerRow = page.getByRole("row").filter({ hasText: "student_2026" });
  await expect(learnerRow).toContainText("启用");
  expect(createdPayload).toMatchObject({ username: "student_2026", display_name: "实训学员", role: "learner" });

  await learnerRow.getByRole("button", { name: "停用" }).click();
  await expect(page.getByRole("row").filter({ hasText: "student_2026" })).toContainText("停用");
});
