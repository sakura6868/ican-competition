<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Copy,
  FilePlus2,
  FlaskConical,
  Plus,
  RefreshCw,
  Rocket,
  Save,
  Scale,
  Trash2,
} from "lucide-vue-next";

import { api, ApiError } from "@/api/client";
import type {
  Asset,
  CourseMetadata,
  EvidenceRef,
  JobDetail,
  ProcessVocabulary,
  Sop,
  SopStep,
  SourceGrade,
} from "@/api/types";
import {
  cloneSteps,
  createBlankStep,
  duplicateStep,
  moveStep,
  normalizeWeights,
  removeStep,
  splitTerms,
  validateDraft,
} from "@/services/sopDraft";
import { pollingRetryDelayMs } from "@/services/jobPolling";
import { displayAssetName, displayProcessName } from "@/services/displayNames";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const route = useRoute();
const sops = ref<Sop[]>([]);
const assets = ref<Asset[]>([]);
const videos = ref<Asset[]>([]);
const manuals = ref<Asset[]>([]);
const selected = ref<Sop | null>(null);
const draftSteps = ref<SopStep[]>([]);
const draftSourceGrade = ref<SourceGrade>("video_reference");
const courseMeta = ref<CourseMetadata>({ title: "", description: "", objectives: [], difficulty: "入门", audience: "零基础学习者", estimated_minutes: 10 });
const courseObjectives = ref("");
const vocabulary = ref<ProcessVocabulary | null>(null);
const vocabularyTools = ref("");
const vocabularyObjects = ref("");
const vocabularyActions = ref("");
const vocabularyBusy = ref(false);
const vocabularyDirty = ref(false);
const processKey = ref("impact_grinder_safe_subassembly");
const referenceVideoId = ref("");
const manualIds = ref<string[]>([]);
const error = ref("");
const info = ref("");
const busy = ref(false);
const saving = ref(false);
const publishing = ref(false);
const dirty = ref(false);
const compileJob = ref<JobDetail | null>(null);
const compilePollWarning = ref("");
const teachingDurationMs = ref(0);
let pollTimer: number | null = null;

const editable = computed(() => selected.value?.status === "draft" && auth.isWriter);
const totalWeight = computed(() => draftSteps.value.reduce((sum, step) => sum + (Number(step.weight) || 0), 0));
const issues = computed(() => {
  const result = validateDraft(draftSteps.value, draftSourceGrade.value);
  draftSteps.value.forEach((step, stepIndex) => {
    if (step.teaching || step.verification) {
      if (!step.teaching || step.teaching.end_ms <= step.teaching.start_ms || (teachingDurationMs.value && step.teaching.end_ms > teachingDurationMs.value)) result.push({ stepIndex, message: `步骤 ${step.index} 的教学片段时间无效` });
      if (!step.verification?.teacher_confirmed) result.push({ stepIndex, message: `步骤 ${step.index} 的陪练条件需要教师确认` });
      if (!step.verification?.checks.some(c => c.required && c.mode === "visual" && c.category === "completion")) result.push({ stepIndex, message: `步骤 ${step.index} 缺少视觉完成条件` });
    }
  });
  if (selected.value && (!vocabulary.value?.approved || vocabularyDirty.value)) {
    result.push({ stepIndex: null, message: "发布前需要保存并确认该工序的受控词表" });
  } else if (vocabulary.value) {
    const allowed = {
      tools: new Set(vocabulary.value.tools),
      objects: new Set(vocabulary.value.objects),
      actions: new Set(vocabulary.value.actions),
    };
    draftSteps.value.forEach((step, stepIndex) => {
      (["tools", "objects", "actions"] as const).forEach((field) => {
        const unknown = step[field].filter((term) => !allowed[field].has(term.trim().toLowerCase()));
        if (unknown.length) {
          result.push({ stepIndex, message: `步骤 ${stepIndex + 1} 的词项未登记：${unknown.join("、")}` });
        }
      });
    });
  }
  return result;
});
const compilePercent = computed(() => Math.max(0, Math.min(100, compileJob.value?.progress_percent ?? 0)));
const selectedReferenceVideo = computed(() => videos.value.find((video) => video.id === referenceVideoId.value));
const canCompileSelectedVideo = computed(() => Boolean(selectedReferenceVideo.value?.allow_provider_export));

function stopPolling() {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
}

async function loadAssets() {
  assets.value = await api.get<Asset[]>("/assets");
  videos.value = assets.value.filter((asset) => asset.kind === "video");
  manuals.value = assets.value.filter(
    (asset) => (asset.kind === "manual" || asset.kind === "image")
      && asset.indexed
      && asset.allow_provider_export,
  );
}

async function load(preferredId?: string) {
  sops.value = await api.get<Sop[]>("/sops");
  const next = sops.value.find((sop) => sop.id === preferredId)
    ?? sops.value.find((sop) => sop.id === selected.value?.id)
    ?? sops.value[0]
    ?? null;
  selectSop(next);
}

function selectSop(sop: Sop | null) {
  teachingDurationMs.value = 0;
  selected.value = sop;
  draftSteps.value = cloneSteps(sop?.steps ?? []);
  draftSourceGrade.value = sop?.source_grade ?? "video_reference";
  courseMeta.value = { title: "", description: "", objectives: [], difficulty: "入门", audience: "零基础学习者", estimated_minutes: 10, ...(sop?.course_meta ?? {}) };
  courseObjectives.value = courseMeta.value.objectives.join("\n");
  dirty.value = false;
  error.value = "";
  vocabulary.value = null;
  vocabularyDirty.value = false;
  if (sop) void loadVocabulary(sop.process_key);
}

async function loadVocabulary(key: string) {
  try {
    const loaded = await api.get<ProcessVocabulary>(`/sops/vocabularies/${encodeURIComponent(key)}`);
    if (selected.value?.process_key !== key) return;
    vocabulary.value = loaded;
    vocabularyTools.value = loaded.tools.join("，");
    vocabularyObjects.value = loaded.objects.join("，");
    vocabularyActions.value = loaded.actions.join("，");
    vocabularyDirty.value = false;
  } catch (reason) {
    if (selected.value?.process_key !== key) return;
    vocabulary.value = null;
    vocabularyTools.value = splitTerms(draftSteps.value.flatMap((step) => step.tools).join("，")).join("，");
    vocabularyObjects.value = splitTerms(draftSteps.value.flatMap((step) => step.objects).join("，")).join("，");
    vocabularyActions.value = splitTerms(draftSteps.value.flatMap((step) => step.actions).join("，")).join("，");
    vocabularyDirty.value = true;
    if (!(reason instanceof ApiError && reason.status === 404)) {
      error.value = reason instanceof Error ? reason.message : "受控词表加载失败";
    }
  }
}

function markVocabularyDirty() {
  vocabularyDirty.value = true;
}

async function saveVocabulary() {
  if (!selected.value || !auth.isWriter) return;
  vocabularyBusy.value = true;
  error.value = "";
  try {
    vocabulary.value = await api.patch<ProcessVocabulary>(
      `/sops/vocabularies/${encodeURIComponent(selected.value.process_key)}`,
      {
        tools: splitTerms(vocabularyTools.value),
        objects: splitTerms(vocabularyObjects.value),
        actions: splitTerms(vocabularyActions.value),
        approved: true,
      },
    );
    vocabularyDirty.value = false;
    info.value = "受控词表已由教师确认，发布语义门禁已解锁。";
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "受控词表保存失败";
  } finally {
    vocabularyBusy.value = false;
  }
}

async function pollCompileJob(jobId: string, consecutiveFailures = 0) {
  stopPolling();
  try {
    const job = await api.get<JobDetail>(`/jobs/${jobId}`);
    compilePollWarning.value = "";
    compileJob.value = job;
    localStorage.setItem("gx_active_sop_compile_job", jobId);
    if (job.status === "succeeded") {
      localStorage.removeItem("gx_active_sop_compile_job");
      busy.value = false;
      const sopId = typeof job.result?.sop_id === "string" ? job.result.sop_id : undefined;
      await load(sopId);
      info.value = "SOP 草稿已生成，可在下方校对步骤后保存并发布。";
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      busy.value = false;
      error.value = job.error_summary || "SOP 编译任务失败";
      return;
    }
    pollTimer = window.setTimeout(() => void pollCompileJob(jobId), 1000);
  } catch (reason) {
    if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403 || reason.status === 404)) {
      busy.value = false;
      localStorage.removeItem("gx_active_sop_compile_job");
      compilePollWarning.value = "";
      error.value = reason.message;
      return;
    }
    const failures = consecutiveFailures + 1;
    const delay = pollingRetryDelayMs(failures);
    compilePollWarning.value = `编译进度连接中断，${Math.round(delay / 1000)} 秒后自动重连（第 ${failures} 次）。`;
    pollTimer = window.setTimeout(() => void pollCompileJob(jobId, failures), delay);
  }
}

async function retryCompileJob() {
  if (!compileJob.value) return;
  error.value = "";
  info.value = "";
  compilePollWarning.value = "";
  busy.value = true;
  try {
    const retried = await api.post<JobDetail>(`/jobs/${compileJob.value.id}/retry`, {});
    compileJob.value = retried;
    localStorage.setItem("gx_active_sop_compile_job", retried.id);
    info.value = "编译任务已重新入队，将从可恢复检查点继续。";
    await pollCompileJob(retried.id);
  } catch (reason) {
    busy.value = false;
    error.value = reason instanceof Error ? reason.message : "重新编译失败";
  }
}

async function onCompile() {
  error.value = "";
  info.value = "";
  busy.value = true;
  compileJob.value = null;
  compilePollWarning.value = "";
  const payload = {
    process_key: processKey.value.trim(),
    reference_video_asset_id: referenceVideoId.value,
    manual_asset_ids: manualIds.value,
    teaching_mode: true,
  };
  try {
    const queued = await api.post<{ job_id: string; status: string }>("/sops/compile-jobs", payload);
    info.value = "已提交任务，正在探测视频。";
    await pollCompileJob(queued.job_id);
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 404) {
      try {
        const sop = await api.post<Sop>("/sops/compile", payload);
        await load(sop.id);
        info.value = `SOP v${sop.version} 草稿已生成。`;
      } catch (fallbackReason) {
        error.value = fallbackReason instanceof Error ? fallbackReason.message : "编译失败";
      } finally {
        busy.value = false;
      }
      return;
    }
    busy.value = false;
    error.value = reason instanceof Error ? reason.message : "编译失败";
  }
}

function markDirty() {
  dirty.value = true;
}

function syncVerificationPolicy(step: SopStep) {
  if (!step.verification) return;
  const hasRequiredSafetyCheck = step.verification.checks.some(check => check.required && check.category === "safety");
  step.verification.confirm_windows = step.critical || hasRequiredSafetyCheck ? 2 : 1;
  step.verification.teacher_confirmed = false;
}

function updateTerms(step: SopStep, field: "tools" | "objects" | "actions", value: string) {
  step[field] = splitTerms(value);
  markDirty();
}

function updateChecks(step: SopStep, field: "completion_checks" | "observable_checks", value: string) {
  step[field] = splitTerms(value);
  if (step.verification) {
    const category = field === "completion_checks" ? "completion" : "safety";
    const previous = step.verification.checks;
    step.verification.checks = [
      ...previous.filter(c => c.mode !== "visual" || c.category !== category),
      ...step[field].map((criterion, index) => ({ id: `${field}_${index + 1}`, criterion, category, mode: "visual", required: true, correction: previous.find(c => c.criterion === criterion)?.correction ?? "" }) as NonNullable<SopStep["verification"]>["checks"][number]),
    ];
    step.verification.teacher_confirmed = false;
  }
  markDirty();
}

function enableTutoring(step: SopStep) {
  const assetId = selected.value?.reference_asset_id;
  if (!assetId) { error.value = "请先绑定标准教学视频。"; return; }
  const evidenceTimes = step.evidence_refs.filter(r => r.kind === "video" && r.timestamp_ms != null).map(r => r.timestamp_ms as number);
  const start = evidenceTimes.length ? Math.min(...evidenceTimes) : 0;
  step.teaching = { source_asset_id: assetId, start_ms: Math.max(0, start), end_ms: teachingDurationMs.value || Math.max(1000, step.expected_duration_s * 1000), instruction: step.name, reference_timestamps_ms: [] };
  step.verification = { confirm_windows: step.critical || step.observable_checks.length ? 2 : 1, teacher_confirmed: false, checks: [
    ...(step.completion_checks ?? []).map((criterion, i) => ({ id: `completion_checks_${i + 1}`, criterion, category: "completion" as const, mode: "visual" as const, required: true, correction: "" })),
    ...step.observable_checks.map((criterion, i) => ({ id: `observable_checks_${i + 1}`, criterion, category: "safety" as const, mode: "visual" as const, required: true, correction: "" })),
    ...step.unobservable_checks.map((criterion, i) => ({ id: `unobservable_checks_${i + 1}`, criterion, category: "completion" as const, mode: "manual" as const, required: true, correction: "" })),
  ] };
  markDirty();
}

function syncTutoring(step: SopStep) {
  if (!step.verification) return;
  syncVerificationPolicy(step);
  step.completion_checks = step.verification.checks.filter(c => c.mode === "visual" && c.category === "completion").map(c => c.criterion);
  step.observable_checks = step.verification.checks.filter(c => c.mode === "visual" && c.category === "safety").map(c => c.criterion);
  step.unobservable_checks = step.verification.checks.filter(c => c.mode === "manual").map(c => c.criterion);
  markDirty();
}

function updateTeachingRange(step: SopStep, field: "start_ms" | "end_ms", seconds: string) {
  if (!step.teaching) return;
  step.teaching[field] = Math.round(Number(seconds) * 1000);
  step.teaching.reference_timestamps_ms = [];
  syncTutoring(step);
}

function addTeachingCheck(step: SopStep) {
  step.verification?.checks.push({ id: `check_${crypto.randomUUID().replaceAll('-', '')}`, criterion: "", category: "completion", mode: "visual", required: true, correction: "" });
  syncTutoring(step);
}

function previewStep(step: SopStep) {
  const video = document.getElementById("teaching-preview") as HTMLVideoElement | null;
  if (video && step.teaching) { video.currentTime = step.teaching.start_ms / 1000; void video.play().catch(() => {}); }
}

function availableEvidenceAssets(kind: EvidenceRef["kind"]): Asset[] {
  const linked = new Set(selected.value?.source_asset_ids ?? []);
  return assets.value.filter((asset) => linked.has(asset.id) && (
    kind === "video" ? asset.kind === "video" : kind === "manual" ? asset.kind === "manual" : false
  ));
}

function addEvidence(step: SopStep) {
  const video = availableEvidenceAssets("video")[0];
  step.evidence_refs.push({
    kind: "video",
    ref: "",
    description: "教师校对的来源证据",
    asset_id: video?.id ?? selected.value?.reference_asset_id ?? null,
  });
  markDirty();
}

function removeEvidence(step: SopStep, position: number) {
  step.evidence_refs.splice(position, 1);
  markDirty();
}

function changeEvidenceKind(evidence: EvidenceRef) {
  evidence.asset_id = availableEvidenceAssets(evidence.kind)[0]?.id ?? null;
  evidence.timestamp_ms = null;
  evidence.page = null;
  evidence.section = null;
  evidence.chunk_id = null;
  evidence.chunk_index = null;
  evidence.content_hash = null;
  evidence.ref = evidence.kind === "sensor" ? "sensor#" : "";
  markDirty();
}

function togglePredecessor(step: SopStep, predecessorIndex: number, checked: boolean) {
  const next = new Set(step.predecessors);
  if (checked) next.add(predecessorIndex);
  else next.delete(predecessorIndex);
  step.predecessors = [...next].sort((a, b) => a - b);
  markDirty();
}

function addStep() {
  draftSteps.value = [...draftSteps.value, createBlankStep(draftSteps.value.length + 1)];
  draftSteps.value = normalizeWeights(draftSteps.value);
  markDirty();
}

function copyStep(position: number) {
  draftSteps.value = normalizeWeights(duplicateStep(draftSteps.value, position, `step_${Date.now()}_${position + 2}`));
  markDirty();
}

function deleteStep(position: number) {
  draftSteps.value = normalizeWeights(removeStep(draftSteps.value, position));
  markDirty();
}

function shiftStep(position: number, delta: number) {
  draftSteps.value = moveStep(draftSteps.value, position, position + delta);
  markDirty();
}

function normalize() {
  draftSteps.value = normalizeWeights(draftSteps.value);
  markDirty();
}

function focusIssue(stepIndex: number | null) {
  if (stepIndex == null) return;
  document.getElementById(`sop-step-${stepIndex}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  window.setTimeout(() => document.getElementById(`sop-step-name-${stepIndex}`)?.focus(), 250);
}

async function saveDraft(): Promise<Sop | null> {
  if (!selected.value || !auth.isWriter) return null;
  if (issues.value.length) {
    error.value = "请先处理发布检查中的问题。";
    focusIssue(issues.value[0].stepIndex);
    return null;
  }
  saving.value = true;
  error.value = "";
  const previousId = selected.value.id;
  try {
    const saved = await api.patch<Sop>(`/sops/${previousId}`, {
      steps: draftSteps.value,
      source_grade: draftSourceGrade.value,
      course_meta: { ...courseMeta.value, objectives: splitTerms(courseObjectives.value.replaceAll("\n", "，")) },
    });
    await load(saved.id);
    info.value = saved.id === previousId ? "草稿已保存。" : `已创建可编辑的 v${saved.version} 草稿。`;
    return saved;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "保存失败";
    return null;
  } finally {
    saving.value = false;
  }
}

async function forkPublished() {
  if (!selected.value) return;
  dirty.value = true;
  await saveDraft();
}

async function onPublish() {
  if (!selected.value || publishing.value) return;
  error.value = "";
  if (issues.value.length) {
    error.value = "发布前还有检查项未通过。";
    focusIssue(issues.value[0].stepIndex);
    return;
  }
  const saved = dirty.value ? await saveDraft() : selected.value;
  if (!saved) return;
  publishing.value = true;
  try {
    const published = await api.post<Sop>(`/sops/${saved.id}/publish`);
    await load(published.id);
      info.value = `${displayProcessName(published.process_key)} v${published.version} 已发布，学员现在可以选择它进行考评。`;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "发布失败";
  } finally {
    publishing.value = false;
  }
}

onMounted(async () => {
  try {
    await Promise.all([load(), loadAssets()]);
    if (typeof route.query.asset === "string") {
      const routedVideo = videos.value.find((video) => video.id === route.query.asset);
      if (routedVideo?.allow_provider_export) {
        referenceVideoId.value = routedVideo.id;
        info.value = "已从知识库带入标准教学视频，可以直接生成 SOP 草稿。";
      } else if (routedVideo) {
        error.value = "该视频未授权外部 AI 关键帧分析，请重新上传并明确授权。";
      }
    }
    const activeJob = localStorage.getItem("gx_active_sop_compile_job");
    if (activeJob) {
      busy.value = true;
      await pollCompileJob(activeJob);
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "页面加载失败";
  }
});
onBeforeUnmount(stopPolling);
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div class="head-led" aria-hidden="true"></div>
      <div>
        <h1>SOP 课程编排</h1>
        <p class="muted">把标准教学视频编译成步骤课程，校对完成条件后用于实时陪练与考评。</p>
      </div>
    </header>

    <p v-if="error" class="alert alert--err" role="alert"><span class="alert-led"></span>{{ error }}</p>
    <p v-if="info" class="alert alert--info" role="status"><span class="alert-led"></span>{{ info }}</p>

    <form class="panel compile-panel" @submit.prevent="onCompile">
      <div class="panel-label">01 · 从标准视频生成草稿</div>
      <div class="compile-grid">
        <label class="field">
          <span>工序标识</span>
          <input v-model="processKey" required type="text" data-testid="process-key" />
        </label>
        <label class="field">
          <span>标准教学视频</span>
          <select v-model="referenceVideoId" required>
            <option value="">选择视频</option>
            <option v-for="video in videos" :key="video.id" :value="video.id" :disabled="!video.allow_provider_export">{{ displayAssetName(video.filename) }}{{ video.allow_provider_export ? "" : "（未授权外部 AI）" }}</option>
          </select>
        </label>
        <label class="field">
          <span>关联手册（可选）</span>
          <select v-model="manualIds" multiple class="multi-select">
            <option v-for="manual in manuals" :key="manual.id" :value="manual.id">{{ displayAssetName(manual.filename) }}</option>
          </select>
        </label>
        <button class="gx-btn gx-btn--primary" type="submit" :disabled="busy || !auth.isWriter || !canCompileSelectedVideo">
          <FlaskConical :size="17" />{{ busy ? "正在编译" : "生成 SOP 草稿" }}
        </button>
      </div>
      <div v-if="compileJob || compilePollWarning" class="job-progress" aria-live="polite">
        <div class="progress-copy"><span>{{ compilePollWarning || compileJob?.status_text || compileJob?.stage || "等待处理" }}</span><strong>{{ compilePercent }}%</strong></div>
        <div class="progress-track"><span :style="{ width: `${compilePercent}%` }"></span></div>
        <button v-if="compileJob && (compileJob.status === 'failed' || compileJob.status === 'cancelled')" type="button" class="gx-btn retry-compile" :disabled="busy" @click="retryCompileJob"><RefreshCw :size="16" />重试编译</button>
      </div>
    </form>

    <section v-if="selected?.reference_asset_id" class="gx-panel teaching-source">
      <div><h3>标准教学视频 · 校对步骤时间</h3>
        <video id="teaching-preview" :key="selected.reference_asset_id" :src="`/api/v1/assets/${selected.reference_asset_id}/preview`" controls preload="metadata" @loadedmetadata="teachingDurationMs = Math.round(($event.target as HTMLVideoElement).duration * 1000)"></video>
      </div>
      <aside class="source-guidance"><span>让示范成为可以练习的课程</span><h4>发布前，做好这三项校对</h4><ol><li><strong>步骤清楚</strong><p>每段示范对应一个学习目标，起止时间覆盖完整操作。</p></li><li><strong>结果可见</strong><p>完成条件能从镜头中确认；无法观察的条件交给教师核验。</p></li><li><strong>反馈具体</strong><p>告诉学员哪里出错、怎样修正，逐步确认后再发布课程。</p></li></ol></aside>
    </section>
    <div class="studio-layout">
      <aside class="panel version-pane">
        <div class="panel-label">02 · 选择版本</div>
        <button v-for="sop in sops" :key="sop.id" type="button" class="sop-row" :class="{ active: selected?.id === sop.id }" @click="selectSop(sop)">
          <span class="sop-led" :class="`sop-led--${sop.status}`"></span>
          <span><strong>{{ sop.course_meta?.title || displayProcessName(sop.process_key) }}</strong><small>v{{ sop.version }} · {{ sop.steps.length }} 步 · {{ sop.status }}</small></span>
        </button>
        <p v-if="!sops.length" class="empty">尚无 SOP 草稿</p>
      </aside>

      <main v-if="selected" class="editor-pane">
        <section class="panel editor-head">
          <div>
            <div class="panel-label">03 · 校对并发布</div>
            <h2>{{ courseMeta.title || displayProcessName(selected.process_key) }} <span>v{{ selected.version }}</span></h2>
            <p>{{ selected.status === "draft" ? (dirty ? "有未保存修改" : "草稿已同步") : "已发布版本只读" }}</p>
            <label class="field source-grade-field">
              <span>来源等级</span>
              <select v-model="draftSourceGrade" :disabled="!editable" @change="markDirty">
                <option value="video_reference">标准视频参考</option>
                <option value="manual_backed">手册支撑</option>
                <option value="sensor_verified">传感器核验</option>
              </select>
            </label>
          </div>
          <div class="head-actions">
            <button v-if="selected.status === 'published' && auth.isWriter" type="button" class="gx-btn" :disabled="saving || publishing" @click="forkPublished"><FilePlus2 :size="17" />创建新草稿</button>
            <template v-else-if="editable">
              <button type="button" class="gx-btn" @click="normalize"><Scale :size="17" />权重归一</button>
              <button type="button" class="gx-btn" :disabled="saving || publishing || !dirty" @click="saveDraft"><Save :size="17" />保存草稿</button>
              <button type="button" class="gx-btn gx-btn--primary" :disabled="saving || publishing || issues.length > 0" @click="onPublish"><Rocket :size="17" />{{ publishing ? "正在发布" : "发布版本" }}</button>
            </template>
          </div>
        </section>

        <section class="panel course-meta-panel">
          <div class="panel-label">课程信息</div>
          <div class="course-meta-grid">
            <label class="field field--wide"><span>课程名称</span><input v-model="courseMeta.title" :disabled="!editable" placeholder="例如：三分钟学会系鞋带" @input="markDirty" /></label>
            <label class="field"><span>难度</span><select v-model="courseMeta.difficulty" :disabled="!editable" @change="markDirty"><option>入门</option><option>进阶</option><option>挑战</option></select></label>
            <label class="field"><span>预计学习时长（分钟）</span><input v-model.number="courseMeta.estimated_minutes" :disabled="!editable" min="1" max="600" type="number" @input="markDirty" /></label>
            <label class="field field--wide"><span>适合人群</span><input v-model="courseMeta.audience" :disabled="!editable" @input="markDirty" /></label>
            <label class="field field--full"><span>课程简介</span><textarea v-model="courseMeta.description" :disabled="!editable" rows="2" @input="markDirty" /></label>
            <label class="field field--full"><span>学习目标（每行一项）</span><textarea v-model="courseObjectives" :disabled="!editable" rows="3" @input="markDirty" /></label>
          </div>
        </section>

        <section class="panel vocabulary-panel">
          <div class="vocabulary-head">
            <div>
              <div class="panel-label">受控词表</div>
              <h3>限定本工序允许出现的工具、对象和动作</h3>
            </div>
            <span class="vocabulary-status" :class="{ approved: vocabulary?.approved && !vocabularyDirty }">
              {{ vocabulary?.approved && !vocabularyDirty ? "教师已确认" : "待教师确认" }}
            </span>
          </div>
          <div class="vocabulary-grid">
            <label class="field"><span>工具词</span><textarea v-model="vocabularyTools" rows="3" :disabled="!editable" @input="markVocabularyDirty" /></label>
            <label class="field"><span>对象词</span><textarea v-model="vocabularyObjects" rows="3" :disabled="!editable" @input="markVocabularyDirty" /></label>
            <label class="field"><span>动作词</span><textarea v-model="vocabularyActions" rows="3" :disabled="!editable" @input="markVocabularyDirty" /></label>
          </div>
          <button v-if="editable" type="button" class="gx-btn vocabulary-save" :disabled="vocabularyBusy || (!vocabularyDirty && vocabulary?.approved)" @click="saveVocabulary">
            <CheckCircle2 :size="17" />{{ vocabularyBusy ? "正在保存" : "保存并确认词表" }}
          </button>
        </section>

        <section v-if="editable" class="panel validation-panel" :class="{ valid: issues.length === 0 }">
          <div class="validation-summary">
            <CheckCircle2 :size="18" /><span v-if="issues.length === 0">发布检查已通过</span><span v-else>还有 {{ issues.length }} 项需要处理</span><strong>总权重 {{ totalWeight.toFixed(2) }} / 100</strong>
          </div>
          <button v-for="issue in issues" :key="issue.message" type="button" class="issue" @click="focusIssue(issue.stepIndex)">{{ issue.message }}</button>
        </section>

        <section class="steps-list" aria-label="SOP 步骤编辑器">
          <article v-for="(step, position) in draftSteps" :id="`sop-step-${position}`" :key="step.id" class="panel step-editor">
            <div class="step-rail">
              <span>{{ String(step.index).padStart(2, "0") }}</span>
              <button type="button" title="上移" aria-label="上移步骤" :disabled="!editable || position === 0" @click="shiftStep(position, -1)"><ArrowUp :size="16" /></button>
              <button type="button" title="下移" aria-label="下移步骤" :disabled="!editable || position === draftSteps.length - 1" @click="shiftStep(position, 1)"><ArrowDown :size="16" /></button>
            </div>
            <div class="step-fields">
              <label class="field field--wide"><span>步骤名称</span><input :id="`sop-step-name-${position}`" v-model="step.name" :disabled="!editable" @input="markDirty" /></label>
              <label class="field"><span>预计时长（秒）</span><input v-model.number="step.expected_duration_s" :disabled="!editable" min="1" type="number" @input="markDirty" /></label>
              <label class="field"><span>权重</span><input v-model.number="step.weight" :disabled="!editable" min="0.01" step="0.01" type="number" @input="markDirty" /></label>
              <label class="field field--wide"><span>工具（逗号分隔）</span><input :value="step.tools.join('，')" :disabled="!editable" @input="updateTerms(step, 'tools', ($event.target as HTMLInputElement).value)" /></label>
              <label class="field field--wide"><span>操作对象（逗号分隔）</span><input :value="step.objects.join('，')" :disabled="!editable" @input="updateTerms(step, 'objects', ($event.target as HTMLInputElement).value)" /></label>
              <label class="field field--wide"><span>动作词（逗号分隔）</span><input :value="step.actions.join('，')" :disabled="!editable" @input="updateTerms(step, 'actions', ($event.target as HTMLInputElement).value)" /></label>
              <label class="field field--full"><span>完成状态（每行一项）</span><textarea :value="(step.completion_checks ?? []).join('\n')" :disabled="!editable" rows="2" @input="updateChecks(step, 'completion_checks', ($event.target as HTMLTextAreaElement).value)"></textarea></label>
              <label class="field field--full"><span>可观察安全规范（每行一项）</span><textarea :value="step.observable_checks.join('\n')" :disabled="!editable" rows="2" @input="updateChecks(step, 'observable_checks', ($event.target as HTMLTextAreaElement).value)"></textarea></label>
              <section class="teaching-editor field--full">
                <div class="evidence-head"><span>逐步教学与实时核验</span><button v-if="editable && !step.verification" class="gx-btn" @click="enableTutoring(step)">启用本步骤陪练</button></div>
                <template v-if="step.teaching && step.verification">
                  <div class="teaching-range"><label class="field"><span>片段开始（秒）</span><input :value="step.teaching.start_ms / 1000" type="number" min="0" step="0.1" :disabled="!editable" @input="updateTeachingRange(step, 'start_ms', ($event.target as HTMLInputElement).value)" /></label><label class="field"><span>片段结束（秒）</span><input :value="step.teaching.end_ms / 1000" type="number" min="0" step="0.1" :disabled="!editable" @input="updateTeachingRange(step, 'end_ms', ($event.target as HTMLInputElement).value)" /></label><button class="gx-btn" @click="previewStep(step)">定位示范片段</button></div>
                  <label class="field"><span>给学员的操作说明</span><textarea v-model="step.teaching.instruction" rows="2" :disabled="!editable" @input="syncTutoring(step)"></textarea></label>
                  <div v-for="(check, checkIndex) in step.verification.checks" :key="check.id" class="teaching-check">
                    <label class="field"><span>完成条件</span><input v-model="check.criterion" :disabled="!editable" @input="syncTutoring(step)" /></label>
                    <label class="field"><span>核验方式</span><select v-model="check.mode" :disabled="!editable" @change="syncTutoring(step)"><option value="visual">摄像头可观察</option><option value="manual">教师人工确认</option></select></label>
                    <label class="field"><span>纠错提示</span><input v-model="check.correction" placeholder="出现明确错误后，指导学员怎样修正" :disabled="!editable" @input="syncTutoring(step)" /></label>
                    <label class="required-check"><input v-model="check.required" type="checkbox" :disabled="!editable" @change="syncTutoring(step)" />必需</label>
                    <button v-if="editable" class="gx-btn" aria-label="删除核验条件" @click="step.verification.checks.splice(checkIndex, 1); syncTutoring(step)"><Trash2 :size="14" /></button>
                  </div>
                  <button v-if="editable" class="gx-btn" @click="addTeachingCheck(step)"><Plus :size="14" />增加核验条件</button>
                  <label class="teacher-confirm"><input v-model="step.verification.teacher_confirmed" type="checkbox" :disabled="!editable" @change="markDirty" />我已校对本步骤片段、标准状态和全部核验条件</label>
                </template>
                <p v-else class="gx-muted">启用后可配置标准片段与核验条件，用于实时陪练。</p>
              </section>
              <section class="evidence-editor field--full">
                <div class="evidence-head"><span>来源证据</span><button v-if="editable" type="button" class="gx-btn" @click="addEvidence(step)"><Plus :size="15" />增加证据</button></div>
                <div v-for="(evidence, evidencePosition) in step.evidence_refs" :key="`${step.id}-evidence-${evidencePosition}`" class="evidence-row">
                  <label class="field"><span>类型</span><select v-model="evidence.kind" :disabled="!editable" @change="changeEvidenceKind(evidence)"><option value="video">视频</option><option value="manual">手册</option><option value="sensor">传感器</option></select></label>
                  <label v-if="evidence.kind !== 'sensor'" class="field"><span>来源资产</span><select v-model="evidence.asset_id" :disabled="!editable" @change="markDirty"><option value="">选择资产</option><option v-for="asset in availableEvidenceAssets(evidence.kind)" :key="asset.id" :value="asset.id">{{ displayAssetName(asset.filename) }}</option></select></label>
                  <label class="field evidence-ref"><span>定位符</span><input v-model="evidence.ref" :disabled="!editable" :placeholder="evidence.kind === 'video' ? 'video@12000ms 或 00:12-00:18' : evidence.kind === 'manual' ? 'manual@资产#chunk=1' : 'sensor#cal-01'" @input="markDirty" /></label>
                  <template v-if="evidence.kind === 'manual'">
                    <label class="field"><span>页码</span><input v-model.number="evidence.page" min="1" type="number" :disabled="!editable" @input="markDirty" /></label>
                    <label class="field"><span>章节</span><input v-model="evidence.section" :disabled="!editable" @input="markDirty" /></label>
                    <label class="field"><span>索引块 ID</span><input v-model.number="evidence.chunk_id" min="1" type="number" :disabled="!editable" @input="markDirty" /></label>
                    <label class="field"><span>块序号</span><input v-model.number="evidence.chunk_index" min="0" type="number" :disabled="!editable" @input="markDirty" /></label>
                    <label class="field evidence-hash"><span>内容 SHA256</span><input v-model="evidence.content_hash" :disabled="!editable" @input="markDirty" /></label>
                  </template>
                  <label class="field evidence-description"><span>证据说明</span><input v-model="evidence.description" :disabled="!editable" @input="markDirty" /></label>
                  <button v-if="editable" type="button" class="evidence-delete" title="删除证据" aria-label="删除证据" @click="removeEvidence(step, evidencePosition)"><Trash2 :size="16" /></button>
                </div>
                <p v-if="!step.evidence_refs.length" class="empty">该步骤尚未绑定来源证据</p>
              </section>
              <fieldset class="predecessors field--full" :disabled="!editable">
                <legend>前置步骤</legend>
                <label v-for="candidate in draftSteps.filter((item) => item.id !== step.id)" :key="candidate.id"><input type="checkbox" :checked="step.predecessors.includes(candidate.index)" @change="togglePredecessor(step, candidate.index, ($event.target as HTMLInputElement).checked)" />{{ candidate.index }} · {{ candidate.name }}</label>
                <span v-if="draftSteps.length === 1">当前没有其他步骤</span>
              </fieldset>
              <div class="step-flags field--full"><label><input v-model="step.critical" type="checkbox" :disabled="!editable" @change="syncVerificationPolicy(step); markDirty()" />关键步骤</label><label><input v-model="step.optional" type="checkbox" :disabled="!editable" @change="markDirty" />可选步骤</label></div>
              <div v-if="step.evidence_refs.length" class="source-refs field--full"><span v-for="ref in step.evidence_refs" :key="`${ref.kind}-${ref.ref}`">{{ ref.kind }} · {{ ref.ref }}<template v-if="ref.page"> · p.{{ ref.page }}</template><template v-if="ref.section"> · {{ ref.section }}</template></span></div>
            </div>
            <div v-if="editable" class="step-actions">
              <button type="button" title="复制步骤" aria-label="复制步骤" @click="copyStep(position)"><Copy :size="17" /></button>
              <button type="button" class="danger" title="删除步骤" aria-label="删除步骤" :disabled="draftSteps.length <= 1" @click="deleteStep(position)"><Trash2 :size="17" /></button>
            </div>
          </article>
        </section>
        <button v-if="editable" type="button" class="add-step" @click="addStep"><Plus :size="18" />新增步骤</button>
      </main>
      <main v-else class="panel empty-editor">选择一个 SOP 版本，或先从标准视频生成草稿。</main>
    </div>
  </section>
</template>

<style scoped>
.teaching-source{margin-bottom:16px;display:grid;grid-template-columns:minmax(0,1.35fr) minmax(250px,1fr);gap:30px;padding:22px}.teaching-source h3{font-size:14px;margin:0}.source-guidance{align-self:center;padding:8px 12px}.source-guidance>span{font-size:10px;color:var(--gx-accent)}.source-guidance h4{margin:8px 0 16px;font-size:17px}.source-guidance ol{padding-left:19px;margin:0;color:var(--gx-accent)}.source-guidance li{padding-left:4px;margin-top:13px;font-size:12px}.source-guidance strong{color:var(--gx-text);font-weight:600}.source-guidance p{font-size:11px;line-height:1.9;color:var(--gx-text-muted);margin:4px 0 0}@media(max-width:1100px){.teaching-source{grid-template-columns:1fr}.source-guidance{padding:0}.source-guidance ol{display:flex;gap:30px;flex-wrap:wrap}.source-guidance li{flex:1;min-width:180px}}.teaching-source video{display:block;max-width:620px;width:100%;max-height:280px;margin-top:12px;border-radius:6px;background:#101318}.teaching-editor{display:grid;gap:12px;padding:14px;border:1px solid var(--gx-accent);border-radius:8px;background:rgba(255,181,71,.04)}.teaching-range{display:flex;gap:12px;align-items:end;flex-wrap:wrap}.teaching-check{display:grid;grid-template-columns:minmax(140px,1fr) 130px minmax(160px,1fr) 65px 44px;gap:8px;align-items:end}.teacher-confirm,.required-check{display:flex;align-items:center;gap:8px;font-size:12px;line-height:1.6}.teacher-confirm{color:var(--gx-accent);min-height:38px}.required-check{min-height:38px}@media(max-width:1000px){.teaching-check{grid-template-columns:1fr 1fr}.teaching-check .field:first-child{grid-column:1/-1}}
.page { max-width: 1420px; }.page-head { display:flex;gap:14px;align-items:flex-start;margin-bottom:20px }.page-head h1 { margin:0 0 4px;font:700 26px var(--gx-font-display) }.muted,.editor-head p,.empty,.empty-editor { color:var(--gx-text-muted);font-size:13px }.head-led,.alert-led,.sop-led { width:9px;height:9px;border-radius:50%;flex:0 0 auto }.head-led { margin-top:8px;background:var(--gx-accent);box-shadow:0 0 9px var(--gx-accent) }.panel { background:linear-gradient(180deg,var(--gx-panel-hi),var(--gx-panel) 7%);border:1px solid var(--gx-panel-edge);border-radius:9px;box-shadow:inset 0 1px rgba(255,255,255,.06),0 2px 10px rgba(0,0,0,.32);padding:16px 18px }.panel-label { margin-bottom:12px;color:var(--gx-text-muted);font:10px var(--gx-font-mono);letter-spacing:1.3px;text-transform:uppercase }.alert { display:flex;align-items:center;gap:9px;padding:11px 14px;border:1px solid;border-radius:7px;font-size:13px }.alert--err { color:var(--gx-danger);border-color:rgba(255,92,92,.3);background:rgba(255,92,92,.08) }.alert--info { color:var(--gx-success);border-color:rgba(61,220,132,.28);background:rgba(61,220,132,.07) }.alert--err .alert-led { background:var(--gx-danger) }.alert--info .alert-led { background:var(--gx-success) }.compile-panel { margin:14px 0 16px }.compile-grid { display:grid;grid-template-columns:minmax(190px,.8fr) minmax(230px,1fr) minmax(220px,1fr) auto;gap:12px;align-items:end }.field { min-width:0;display:flex;flex-direction:column;gap:5px }.field>span,.predecessors legend { color:var(--gx-text-muted);font:10px var(--gx-font-mono);letter-spacing:.7px;text-transform:uppercase }.field input,.field select,.field textarea { width:100%;min-height:44px }.multi-select { min-height:70px;background-image:none;padding-right:8px }.gx-btn { min-height:44px;display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:10px 15px;border:1px solid var(--gx-panel-edge);border-radius:7px;color:var(--gx-text);background:linear-gradient(180deg,var(--gx-panel-hi),var(--gx-panel));cursor:pointer }.gx-btn--primary { color:#291a00;border-color:var(--gx-accent-press);background:linear-gradient(180deg,var(--gx-accent),var(--gx-accent-press));font-weight:700 }.gx-btn:disabled,.step-rail button:disabled,.step-actions button:disabled { opacity:.38;cursor:default }.job-progress { margin-top:14px }.progress-copy { display:flex;justify-content:space-between;gap:12px;margin-bottom:6px;font-size:12px }.progress-track { height:7px;overflow:hidden;border-radius:4px;background:var(--gx-panel-lo) }.progress-track span { display:block;height:100%;background:var(--gx-accent);transition:width 180ms ease }.studio-layout { display:grid;grid-template-columns:250px minmax(0,1fr);gap:16px }.version-pane { align-self:start;position:sticky;top:16px }.sop-row { width:100%;min-height:58px;display:flex;align-items:center;gap:10px;padding:8px 10px;margin-bottom:6px;text-align:left;color:var(--gx-text);border:1px solid transparent;border-radius:7px;background:var(--gx-panel-lo);cursor:pointer }.sop-row.active { border-color:var(--gx-accent);background:rgba(255,181,71,.09) }.sop-row>span:last-child { min-width:0;display:flex;flex-direction:column;gap:3px }.sop-row strong { overflow:hidden;text-overflow:ellipsis }.sop-row small { color:var(--gx-text-muted);font:10px var(--gx-font-mono) }.sop-led--draft { background:var(--gx-led-amber);box-shadow:0 0 5px var(--gx-led-amber) }.sop-led--published { background:var(--gx-led-green);box-shadow:0 0 5px var(--gx-led-green) }.sop-led--archived { background:var(--gx-led-grey) }.editor-pane { min-width:0;display:grid;gap:12px }.editor-head { display:flex;justify-content:space-between;align-items:flex-start;gap:16px }.editor-head h2 { margin:0;color:var(--gx-accent);font:700 20px var(--gx-font-display) }.editor-head h2 span { color:var(--gx-text-muted);font-size:13px }.editor-head p { margin:5px 0 0 }.head-actions { display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end }.validation-panel { border-left:3px solid var(--gx-led-amber) }.validation-panel.valid { border-left-color:var(--gx-led-green) }.validation-summary { min-height:32px;display:flex;align-items:center;gap:8px }.validation-summary strong { margin-left:auto;color:var(--gx-accent) }.issue { display:block;min-height:34px;padding:5px 0;border:0;color:var(--gx-led-amber);background:transparent;text-align:left;cursor:pointer }.steps-list { display:grid;gap:10px }.step-editor { display:grid;grid-template-columns:58px minmax(0,1fr) 42px;gap:14px;padding:14px }.step-rail { display:flex;flex-direction:column;align-items:center;gap:6px;border-right:1px solid var(--gx-panel-edge);padding-right:12px }.step-rail>span { color:var(--gx-accent);font:700 28px var(--gx-font-display) }.step-rail button,.step-actions button { width:36px;height:36px;display:grid;place-items:center;border:1px solid var(--gx-panel-edge);border-radius:6px;color:var(--gx-text);background:var(--gx-panel-lo);cursor:pointer }.step-fields { display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));gap:10px }.field--wide { grid-column:span 2 }.field--full { grid-column:1/-1 }.predecessors { min-width:0;display:flex;flex-wrap:wrap;gap:7px 14px;padding:10px 0 0;border:0;border-top:1px solid rgba(255,255,255,.05) }.predecessors legend { margin-bottom:6px }.predecessors label,.step-flags label { min-height:36px;display:inline-flex;align-items:center;gap:6px;font-size:12px }.predecessors>span { color:var(--gx-text-muted);font-size:12px }.step-flags { display:flex;gap:18px }.source-refs { display:flex;gap:6px;flex-wrap:wrap }.source-refs span { padding:4px 8px;border-radius:4px;color:var(--gx-text-muted);background:var(--gx-panel-lo);font:10px var(--gx-font-mono) }.step-actions { display:flex;flex-direction:column;gap:7px }.step-actions .danger { color:var(--gx-danger) }.add-step { width:100%;min-height:50px;display:flex;align-items:center;justify-content:center;gap:8px;border:1px dashed var(--gx-accent);border-radius:8px;color:var(--gx-accent);background:rgba(255,181,71,.04);cursor:pointer }.empty-editor { min-height:240px;display:grid;place-items:center;text-align:center }
.retry-compile { margin-top:10px;color:var(--gx-accent);border-color:var(--gx-accent) }
.step-rail button,.step-actions button { width:44px;height:44px; }
.source-grade-field { max-width:260px;margin-top:12px }
.vocabulary-head,.evidence-head { display:flex;align-items:center;justify-content:space-between;gap:12px }
.vocabulary-head h3 { margin:0;font-size:15px }
.vocabulary-status { padding:5px 9px;border:1px solid var(--gx-led-amber);border-radius:5px;color:var(--gx-led-amber);font:11px var(--gx-font-mono) }
.vocabulary-status.approved { border-color:var(--gx-led-green);color:var(--gx-led-green) }
.vocabulary-grid { display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px }
.course-meta-grid { display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px }
.vocabulary-save { margin-top:10px }
.evidence-editor { display:grid;gap:8px;padding:10px;border:1px solid var(--gx-panel-edge);border-radius:7px;background:var(--gx-panel-lo) }
.evidence-head>span { color:var(--gx-text-muted);font:10px var(--gx-font-mono);letter-spacing:.7px;text-transform:uppercase }
.evidence-row { display:grid;grid-template-columns:120px minmax(150px,.7fr) minmax(220px,1fr) repeat(2,minmax(100px,.45fr)) 44px;gap:8px;align-items:end;padding-top:9px;border-top:1px solid var(--gx-panel-edge) }
.evidence-ref,.evidence-description,.evidence-hash { grid-column:span 2 }
.evidence-delete { width:44px;height:44px;display:grid;place-items:center;border:1px solid rgba(255,92,92,.3);border-radius:6px;color:var(--gx-danger);background:rgba(255,92,92,.06);cursor:pointer }
.editor-pane { container-type:inline-size; }
@container(max-width:1000px){.step-fields,.evidence-row,.teaching-check{grid-template-columns:repeat(2,minmax(0,1fr))}.teaching-check .field:first-child{grid-column:1/-1}.evidence-delete{grid-column:2;justify-self:end}}
@media(max-width:1100px){.compile-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.step-fields{grid-template-columns:repeat(2,minmax(0,1fr))}.evidence-row{grid-template-columns:repeat(2,minmax(0,1fr))}.evidence-delete{grid-column:2;justify-self:end}}
@media(max-width:760px){.studio-layout,.compile-grid,.vocabulary-grid{grid-template-columns:1fr}.version-pane{position:static}.editor-head{flex-direction:column}.head-actions{width:100%;justify-content:stretch}.head-actions .gx-btn{flex:1}.step-editor{grid-template-columns:46px minmax(0,1fr)}.step-actions{grid-column:1/-1;flex-direction:row;justify-content:flex-end}.step-fields,.evidence-row{grid-template-columns:1fr}.field--wide,.field--full,.evidence-ref,.evidence-description,.evidence-hash{grid-column:1}.evidence-delete{grid-column:1;justify-self:end}.validation-summary{flex-wrap:wrap}.validation-summary strong{width:100%;margin-left:26px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
