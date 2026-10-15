<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  CircleHelp,
  FileVideo2,
  History,
  LoaderCircle,
  PlayCircle,
  RefreshCw,
  Upload,
} from "lucide-vue-next";

import { api, ApiError } from "@/api/client";
import type {
  Asset,
  AssessmentEvidence,
  AssessmentProgress,
  AssessmentSession,
  CoachingFeedback,
  DemoReplayResponse,
  DemoScenario,
  JobDetail,
  ScoreReport,
  SessionReport,
  Sop,
  StepJudgement,
  SubmitAssessmentResponse,
} from "@/api/types";
import EvidencePlayer from "@/components/EvidencePlayer.vue";
import { flowState, jobProgress, resumableJobId } from "@/services/assessmentFlow";
import { demoReplayPath, parseDemoScenario, rejectedRecommendation } from "@/services/demoReplay";
import { evidenceTone, resultLabel } from "@/services/evidence";
import { pollingRetryDelayMs } from "@/services/jobPolling";
import { displayAssetName, displayProcessName } from "@/services/displayNames";
import { useAuthStore } from "@/stores/auth";

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const sessions = ref<AssessmentSession[]>([]);
const videos = ref<Asset[]>([]);
const publishedSops = ref<Sop[]>([]);
const selectedSession = ref<AssessmentSession | null>(null);
const selectedVideoId = ref("");
const selectedSopId = ref("");
const inputMode = ref<"upload" | "existing">("upload");
const selectedFile = ref<File | null>(null);
const providerExportConsent = ref(false);
const providerExportRequired = ref(true);
const runtimeVisionBackend = ref("unknown");
const fileDuration = ref<number | null>(null);
const localPreviewUrl = ref<string | null>(null);
const serverPreviewUrl = ref<string | null>(null);
const progress = ref<AssessmentProgress | null>(null);
const report = ref<ScoreReport | null>(null);
const stepJudgements = ref<StepJudgement[]>([]);
const coaching = ref<CoachingFeedback | null>(null);
const analysisDurationMs = ref<number | null>(null);
const providerMetrics = ref<SessionReport["provider_metrics"]>(null);
const activeEvidenceStep = ref<number | null>(null);
const reportRejected = ref(false);
const reportRejectReason = ref("");
const demoReplay = ref<DemoReplayResponse | null>(null);
const demoLoading = ref(false);
const error = ref("");
const info = ref("");
const pollWarning = ref("");
const submitting = ref(false);
const retrying = ref(false);
const historyOpen = ref(false);
let pollTimer: number | null = null;

const selectedSop = computed(() => publishedSops.value.find((sop) => sop.id === selectedSopId.value));
const selectedVideo = computed(() => videos.value.find((video) => video.id === selectedVideoId.value));
const state = computed(() => flowState(progress.value));
const currentJob = computed(() => progress.value?.job ?? null);
const percent = computed(() => jobProgress(currentJob.value));
const videoSource = computed(() => serverPreviewUrl.value || localPreviewUrl.value);
const hasSelectedInput = computed(() => inputMode.value === "upload" ? Boolean(selectedFile.value) : Boolean(selectedVideoId.value));
const hasRequiredProviderConsent = computed(() => !providerExportRequired.value
  || (inputMode.value === "upload"
    ? providerExportConsent.value
    : Boolean(selectedVideo.value?.allow_provider_export)));
const canSubmit = computed(() => auth.user?.role !== "observer" && Boolean(selectedSop.value) && hasSelectedInput.value && hasRequiredProviderConsent.value && !submitting.value && !["queued", "running"].includes(state.value));
const needsQueueResume = computed(() => Boolean(
  progress.value
  && !progress.value.job
  && (progress.value.session_status === "created" || progress.value.session_status === "ready"),
));
const demoScenario = computed(() => parseDemoScenario(route.query.demo));

const resultText = computed(() => report.value ? resultLabel(report.value.result) : "待分析");
const resultTone = computed(() => report.value ? evidenceTone(report.value.result) : "pending");
const scoreCaption = computed(() => report.value?.result === "inconclusive" ? "临时分数" : "确定性评分");
const recommendations = computed(() => {
  if (reportRejected.value || reportRejectReason.value) {
    return [rejectedRecommendation(reportRejectReason.value)];
  }
  const priority = stepJudgements.value.filter((step) => step.judgement === "failed" || step.judgement === "insufficient_evidence" || step.judgement === "unobservable");
  if (report.value?.result === "inconclusive" && !priority.length) {
    return [rejectedRecommendation("报告未获得足够的步骤证据")];
  }
  if (!priority.length) return ["本次可见步骤均符合 SOP，建议保持当前操作顺序与拍摄视角。"];
  return priority.slice(0, 3).map((step) => `步骤 ${step.step_index}：${step.reason || (step.judgement === "failed" ? "请按 SOP 重新练习该动作。" : "调整机位或减少遮挡后重新提交。")}`);
});

const dimLabels: Record<string, string> = {
  completeness: "步骤完整性", order: "操作顺序", tools_parts: "工具与零件",
  action_quality: "动作规范", efficiency: "操作效率", observable_safety: "可观察安全",
};
const dimRows = computed(() => Object.entries(report.value?.dimension_scores ?? {}).map(([key, value]) => ({ key, label: dimLabels[key] ?? key, value })));

const observableCheckLabels = {
  passed: "已确认",
  failed: "明确违规",
  insufficient_evidence: "证据不足",
} as const;

function observableCheckEntries(step: StepJudgement) {
  return Object.entries(step.observable_check_results ?? {});
}

function stopPolling() {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
}

async function loadBasics() {
  const [allSessions, allAssets, allSops] = await Promise.all([
    api.get<AssessmentSession[]>("/assessment-sessions"),
    api.get<Asset[]>("/assets"),
    api.get<Sop[]>("/sops"),
  ]);
  sessions.value = allSessions.filter((session) => session.mode === "recorded");
  videos.value = allAssets.filter((asset) => asset.kind === "video");
  publishedSops.value = allSops.filter((sop) => sop.status === "published");
  if (!selectedSopId.value && publishedSops.value.length) selectedSopId.value = publishedSops.value[0].id;
  try {
    const capabilities = await api.get<Array<{
      key: string;
      runtime_backend?: string | null;
      provider_export_required?: boolean;
    }>>("/system/capabilities");
    const recorded = capabilities.find((item) => item.key === "recorded_assessment");
    providerExportRequired.value = recorded?.provider_export_required ?? true;
    runtimeVisionBackend.value = recorded?.runtime_backend || "unknown";
  } catch {
    // 状态端点不可用时保守要求授权，避免意外外发。
    providerExportRequired.value = true;
    runtimeVisionBackend.value = "unknown";
  }
}

function clearCurrentResult() {
  progress.value = null;
  report.value = null;
  stepJudgements.value = [];
  serverPreviewUrl.value = null;
  activeEvidenceStep.value = null;
  reportRejected.value = false;
  reportRejectReason.value = "";
  coaching.value = null;
  analysisDurationMs.value = null;
  demoReplay.value = null;
  pollWarning.value = "";
}

function chooseSop(id: string) {
  if (["queued", "running"].includes(state.value)) {
    error.value = "当前视频仍在分析中，请等待完成后再切换 SOP。";
    return;
  }
  stopPolling();
  selectedSession.value = null;
  selectedSopId.value = id;
  clearCurrentResult();
  error.value = "";
  info.value = "";
  localStorage.removeItem("gx_active_assessment_session");
  localStorage.removeItem("gx_active_assessment_job");
  void router.replace({ name: "assessment" });
}

async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0] ?? null;
  selectedFile.value = file;
  providerExportConsent.value = false;
  fileDuration.value = null;
  if (localPreviewUrl.value) URL.revokeObjectURL(localPreviewUrl.value);
  localPreviewUrl.value = file ? URL.createObjectURL(file) : null;
  if (!file || !localPreviewUrl.value) return;
  const probe = document.createElement("video");
  probe.preload = "metadata";
  probe.src = localPreviewUrl.value;
  probe.onloadedmetadata = () => { fileDuration.value = Number.isFinite(probe.duration) ? probe.duration : null; };
}

function setInputMode(mode: "upload" | "existing") {
  inputMode.value = mode;
  error.value = "";
}

async function startAssessment() {
  const sop = selectedSop.value;
  if (!sop) { error.value = "请先选择一个已发布 SOP。"; return; }
  if (inputMode.value === "upload" && !selectedFile.value) { error.value = "请选择操作视频。"; return; }
  if (inputMode.value === "existing" && !selectedVideoId.value) { error.value = "请选择已有视频。"; return; }
  stopPolling();
  clearCurrentResult();
  error.value = "";
  info.value = "";
  submitting.value = true;
  try {
    let session: AssessmentSession;
    let jobId: string;
    if (inputMode.value === "upload" && selectedFile.value) {
      const form = new FormData();
      form.append("file", selectedFile.value);
      form.append("sop_version_id", sop.id);
      form.append("process_key", sop.process_key);
      form.append("allow_provider_export", String(providerExportConsent.value));
      const response = await api.postForm<SubmitAssessmentResponse>("/assessment-sessions/submit-video", form);
      session = response.session;
      jobId = response.job_id;
      serverPreviewUrl.value = response.preview_url;
      info.value = response.cache_hit
        ? "已命中同一用户、视频、SOP 与模型版本的可信缓存，无需重复调用模型。"
        : "视频已提交，页面会自动显示处理进度和最终报告。";
    } else {
      session = await api.post<AssessmentSession>("/assessment-sessions", {
        process_key: sop.process_key, sop_version_id: sop.id, mode: "recorded", input_asset_id: selectedVideoId.value,
      });
      const queued = await api.post<{ job_id: string; status: string }>(`/assessment-sessions/${session.id}/process`, {});
      jobId = queued.job_id;
      serverPreviewUrl.value = `/api/v1/assets/${selectedVideoId.value}/preview`;
    }
    selectedSession.value = session;
    localStorage.setItem("gx_active_assessment_session", session.id);
    localStorage.setItem("gx_active_assessment_job", jobId);
    void router.replace({ name: "assessment", query: { session: session.id } });
    if (!info.value) info.value = "视频已提交，页面会自动显示处理进度和最终报告。";
    await loadBasics();
    await pollAssessment(session.id, jobId);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "提交考评失败";
  } finally {
    submitting.value = false;
  }
}

async function pollAssessment(sessionId: string, fallbackJobId?: string, consecutiveFailures = 0) {
  stopPolling();
  try {
    let next: AssessmentProgress;
    try {
      next = await api.get<AssessmentProgress>(`/assessment-sessions/${sessionId}/progress`);
    } catch (reason) {
      if (!(reason instanceof ApiError) || reason.status !== 404 || !fallbackJobId) throw reason;
      const job = await api.get<JobDetail>(`/jobs/${fallbackJobId}`);
      next = { session_id: sessionId, session_status: job.status, job, report_ready: job.status === "succeeded", report_id: null };
    }
    pollWarning.value = "";
    progress.value = next;
    if (next.report_ready || next.job?.status === "succeeded") {
      localStorage.removeItem("gx_active_assessment_session");
      localStorage.removeItem("gx_active_assessment_job");
      await loadReport(sessionId);
      return;
    }
    if (
      next.job?.status === "failed"
      || next.job?.status === "cancelled"
      || next.session_status === "failed"
      || next.session_status === "cancelled"
    ) {
      error.value = next.job?.error_summary || "分析任务失败，可点击重试。";
      return;
    }
    if (!next.job && (next.session_status === "created" || next.session_status === "ready")) {
      info.value = "该会话尚未进入分析队列，可点击“继续分析”从现有视频恢复。";
      return;
    }
    pollTimer = window.setTimeout(() => void pollAssessment(sessionId, next.job?.id ?? fallbackJobId), 1000);
  } catch (reason) {
    if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403 || reason.status === 404)) {
      localStorage.removeItem("gx_active_assessment_session");
      localStorage.removeItem("gx_active_assessment_job");
      pollWarning.value = "";
      error.value = reason.message;
      return;
    }
    const failures = consecutiveFailures + 1;
    const delay = pollingRetryDelayMs(failures);
    pollWarning.value = `进度连接暂时中断，${Math.round(delay / 1000)} 秒后自动重连（第 ${failures} 次）。`;
    pollTimer = window.setTimeout(
      () => void pollAssessment(sessionId, fallbackJobId, failures),
      delay,
    );
  }
}

async function loadReport(sessionId: string) {
  const data = await api.get<SessionReport>(`/assessment-sessions/${sessionId}/report`);
  report.value = data.report;
  stepJudgements.value = data.steps;
  reportRejected.value = Boolean(data.rejected);
  reportRejectReason.value = data.reject_reason?.trim() ?? "";
  coaching.value = data.coaching ?? null;
  analysisDurationMs.value = data.analysis_duration_ms ?? null;
  providerMetrics.value = data.provider_metrics ?? null;
  if (!data.report) {
    error.value = data.reject_reason || "任务已结束，但报告尚未生成。";
    return;
  }
  try {
    const evidence = await api.get<AssessmentEvidence>(`/assessment-sessions/${sessionId}/evidence`);
    if (evidence.steps.length) {
      stepJudgements.value = evidence.steps.map((step) => {
        const observations = (evidence.observations ?? []).filter((observation) => observation.timestamp_ms >= step.start_ms && observation.timestamp_ms <= step.end_ms);
        return {
          ...step,
          detected_tools: [...new Set(observations.map((item) => item.tool).filter(Boolean))],
          detected_objects: [...new Set(observations.map((item) => item.object).filter(Boolean))],
          detected_actions: [...new Set(observations.map((item) => item.action).filter(Boolean))],
        };
      });
    }
    serverPreviewUrl.value = evidence.preview_url;
  } catch (reason) {
    if (!(reason instanceof ApiError) || reason.status !== 404) throw reason;
    const assetId = selectedSession.value?.input_asset_id;
    if (assetId) serverPreviewUrl.value = `/api/v1/assets/${assetId}/preview`;
  }
  activeEvidenceStep.value = stepJudgements.value[0]?.step_index ?? null;
  info.value = "分析完成，已自动加载步骤证据报告。";
}

async function loadDemoReplay(scenario: DemoScenario) {
  stopPolling();
  clearCurrentResult();
  selectedSession.value = null;
  error.value = "";
  info.value = "";
  demoLoading.value = true;
  try {
    const replay = await api.post<DemoReplayResponse>(demoReplayPath(scenario), {});
    demoReplay.value = replay;
    report.value = replay.report;
    stepJudgements.value = replay.steps;
    serverPreviewUrl.value = replay.preview_url;
    activeEvidenceStep.value = replay.steps[0]?.step_index ?? null;
    reportRejected.value = replay.report.result === "inconclusive";
    reportRejectReason.value = replay.report.result === "inconclusive"
      ? replay.steps.find((step) => step.judgement === "insufficient_evidence" || step.judgement === "unobservable")?.reason ?? "演示场景的可见证据不足"
      : "";
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "无法加载答辩工程回放";
  } finally {
    demoLoading.value = false;
  }
}

async function retryAssessment() {
  const job = currentJob.value;
  const session = selectedSession.value;
  if (!session || retrying.value) return;
  error.value = "";
  info.value = "";
  pollWarning.value = "";
  retrying.value = true;
  try {
    let jobId = job?.id;
    if (jobId) {
      try {
        const retried = await api.post<JobDetail>(`/jobs/${jobId}/retry`, {});
        jobId = retried.id;
      } catch (reason) {
        if (!(reason instanceof ApiError) || reason.status !== 404) throw reason;
      }
    }
    if (!jobId) {
      const queued = await api.post<{ job_id: string }>(`/assessment-sessions/${session.id}/process`, {});
      jobId = queued.job_id;
    }
    await pollAssessment(session.id, jobId);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "重试失败";
  } finally {
    retrying.value = false;
  }
}

async function openHistory(session: AssessmentSession) {
  stopPolling();
  clearCurrentResult();
  selectedSession.value = session;
  if (session.sop_version_id && publishedSops.value.some((sop) => sop.id === session.sop_version_id)) {
    selectedSopId.value = session.sop_version_id;
  }
  error.value = "";
  info.value = "";
  void router.replace({ name: "assessment", query: { session: session.id } });
  if (session.status !== "completed") {
    localStorage.setItem("gx_active_assessment_session", session.id);
    if (session.job_id) localStorage.setItem("gx_active_assessment_job", session.job_id);
    else localStorage.removeItem("gx_active_assessment_job");
  }
  if (session.input_asset_id) serverPreviewUrl.value = `/api/v1/assets/${session.input_asset_id}/preview`;
  try {
    if (session.status === "completed") await loadReport(session.id);
    else await pollAssessment(session.id, session.job_id ?? undefined);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "无法打开历史记录";
  }
}

onMounted(async () => {
  try {
    await loadBasics();
    if (demoScenario.value) {
      await loadDemoReplay(demoScenario.value);
      return;
    }
    const querySessionId = typeof route.query.session === "string" ? route.query.session : null;
    const storedSessionId = localStorage.getItem("gx_active_assessment_session");
    const sessionId = querySessionId || storedSessionId;
    if (sessionId) {
      selectedSession.value = sessions.value.find((session) => session.id === sessionId) ?? null;
      if (!selectedSession.value) {
        error.value = "该考评会话不存在，或当前账号没有访问权限。";
        return;
      }
      if (
        selectedSession.value.sop_version_id
        && publishedSops.value.some((sop) => sop.id === selectedSession.value?.sop_version_id)
      ) {
        selectedSopId.value = selectedSession.value.sop_version_id;
      }
      const jobId = resumableJobId(
        sessionId,
        selectedSession.value.job_id,
        storedSessionId,
        localStorage.getItem("gx_active_assessment_job"),
      );
      if (selectedSession.value.status === "completed") {
        await loadReport(sessionId);
      } else {
        await pollAssessment(sessionId, jobId);
      }
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "考评中心加载失败";
  }
});
watch(demoScenario, async (scenario, previous) => {
  if (scenario === previous) return;
  if (scenario) {
    await loadDemoReplay(scenario);
  } else if (demoReplay.value) {
    clearCurrentResult();
  }
});
onBeforeUnmount(() => {
  stopPolling();
  if (localPreviewUrl.value) URL.revokeObjectURL(localPreviewUrl.value);
});
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div class="head-led"></div>
      <div><h1>学习评估</h1><p>选择教学标准，提交练习视频，生成有步骤证据的学习报告。</p></div>
    </header>

    <section v-if="demoScenario" class="demo-banner replay-banner" data-testid="demo-replay-disclaimer">
      <LoaderCircle v-if="demoLoading" class="spinner" :size="22" />
      <PlayCircle v-else :size="22" />
      <div>
        <strong>{{ demoReplay?.label || "正在加载答辩工程回放" }}</strong>
        <span v-if="demoReplay" class="replay-meta">
          <b>fixture_replay</b><b>缓存工程回放</b><b>{{ demoReplay.quality.sampled_frames }} 帧事件</b>
        </span>
        <p>{{ demoReplay?.disclaimer || "正在通过正式对齐与评分代码生成确定性回放报告。" }}</p>
      </div>
      <RouterLink to="/assessment">切换到真实视频考评</RouterLink>
    </section>
    <p v-if="error" class="alert alert--err" role="alert"><AlertTriangle :size="17" />{{ error }}</p>
    <p v-if="info" class="alert alert--info" role="status"><Check :size="17" />{{ info }}</p>
    <p v-if="pollWarning" class="alert alert--warn" role="status"><RefreshCw class="spinner" :size="17" />{{ pollWarning }}</p>

    <ol v-if="!demoScenario" class="wizard-steps" aria-label="考评流程">
      <li :class="{ active: selectedSop, done: selectedSop }"><span>1</span><div><strong>选择 SOP</strong><small>确认本次考评标准</small></div></li>
      <li :class="{ active: selectedSop && (selectedFile || selectedVideoId), done: selectedFile || selectedVideoId }"><span>2</span><div><strong>提交视频</strong><small>上传新视频或复用资产</small></div></li>
      <li :class="{ active: state === 'queued' || state === 'running', done: state === 'completed' }"><span>3</span><div><strong>自动分析</strong><small>完成后自动打开报告</small></div></li>
    </ol>

    <section v-if="!demoScenario" class="panel submission-panel">
      <div class="submission-column">
        <div class="section-label">01 · 选择已发布 SOP</div>
        <label v-for="sop in publishedSops" :key="sop.id" class="sop-option" :class="{ selected: selectedSopId === sop.id }">
          <input type="radio" name="sop" :value="sop.id" :checked="selectedSopId === sop.id" @change="chooseSop(sop.id)" />
          <span><strong>{{ displayProcessName(sop.process_key) }}</strong><small>版本 {{ sop.version }} · {{ sop.steps.length }} 个步骤</small></span>
          <Check v-if="selectedSopId === sop.id" :size="18" />
        </label>
        <p v-if="!publishedSops.length" class="empty">暂无已发布 SOP，请先由教师发布。</p>
      </div>

      <div class="submission-column">
        <div class="section-label">02 · 选择操作视频</div>
        <div class="segmented">
          <button type="button" :class="{ active: inputMode === 'upload' }" @click="setInputMode('upload')"><Upload :size="16" />上传新视频</button>
          <button type="button" :class="{ active: inputMode === 'existing' }" @click="setInputMode('existing')"><FileVideo2 :size="16" />使用已有视频</button>
        </div>
        <label v-if="inputMode === 'upload'" class="drop-zone">
          <input type="file" accept="video/mp4,video/quicktime,video/webm,.mov,.mp4,.webm" data-testid="assessment-video" @change="onFileChange" />
          <FileVideo2 :size="28" /><strong>{{ selectedFile ? displayAssetName(selectedFile.name) : "选择本地操作视频" }}</strong>
          <span v-if="selectedFile">{{ (selectedFile.size / 1024 / 1024).toFixed(1) }} MB<span v-if="fileDuration"> · {{ fileDuration.toFixed(1) }} 秒</span></span>
          <span v-else>MP4、MOV 或 WebM</span>
        </label>
        <label v-if="inputMode === 'upload' && providerExportRequired" class="provider-consent">
          <input v-model="providerExportConsent" type="checkbox" data-testid="provider-export-consent" />
          <span><strong>允许外部 AI 分析关键帧并生成教学反馈</strong><small>仅发送压缩关键帧和去标识化步骤判定，不发送完整视频、账号信息或总分；未勾选时不会调用外部视觉 API。</small></span>
        </label>
        <label v-else-if="inputMode === 'existing'" class="asset-picker"><span>我的视频资产</span><select v-model="selectedVideoId"><option value="">选择已有视频</option><option v-for="video in videos" :key="video.id" :value="video.id">{{ displayAssetName(video.filename) }}</option></select><small v-if="selectedVideo && providerExportRequired" :class="selectedVideo.allow_provider_export ? 'consent-ok' : 'consent-warning'">{{ selectedVideo.allow_provider_export ? "该资产已授权外部关键帧分析与结构化教学反馈" : "该资产未授权外部分析，请重新上传并明确授权后再分析" }}</small><small v-else-if="selectedVideo" class="consent-ok">当前使用 {{ runtimeVisionBackend }} 本地推理，不需要外部分析授权</small></label>
        <div v-if="selectedSop" class="shooting-tip"><strong>拍摄建议</strong><span>保持工位和双手可见，完整拍摄从准备到结束，遮挡严重时系统会返回无法判断。</span></div>
      </div>

      <div class="submission-action">
        <div class="section-label">03 · 开始分析</div>
        <button type="button" class="primary-action" data-testid="start-assessment" :disabled="!canSubmit" @click="startAssessment"><PlayCircle :size="19" />{{ submitting ? "正在提交" : "提交并开始分析" }}</button>
        <small v-if="inputMode === 'upload' && selectedFile && providerExportRequired && !providerExportConsent">需明确授权外部 AI 关键帧分析后才能提交。</small>
        <small v-else-if="inputMode === 'existing' && selectedVideo && providerExportRequired && !selectedVideo.allow_provider_export">该资产没有外部关键帧分析授权，不能用于当前自动考评。</small>
        <small v-else-if="selectedFile && !providerExportRequired" class="consent-ok">当前使用 {{ runtimeVisionBackend }} 本地推理，视频帧不会发送到外部 Provider。</small>
        <small v-else>刷新页面后可从任务检查点继续，无需重复上传。</small>
      </div>
    </section>

    <section v-if="progress && state !== 'completed'" class="panel progress-panel" aria-live="polite">
      <LoaderCircle v-if="state === 'running' || state === 'queued'" class="spinner" :size="24" />
      <AlertTriangle v-else :size="24" />
      <div class="progress-main"><div><strong>{{ currentJob?.status_text || currentJob?.stage || "等待分析任务" }}</strong><span>{{ state === "failed" ? currentJob?.error_summary : `已分析 ${progress.analyzed_frames ?? 0} 帧${progress.estimated_remaining_seconds != null ? ` · 预计剩余 ${progress.estimated_remaining_seconds} 秒` : ''}` }}</span></div><div class="track"><span :style="{ width: `${percent}%` }"></span></div></div>
      <b>{{ percent }}%</b>
      <button v-if="state === 'failed' || needsQueueResume" type="button" class="retry" :disabled="retrying" @click="retryAssessment"><RefreshCw :size="16" />{{ retrying ? "正在恢复" : needsQueueResume ? "继续分析" : "重试" }}</button>
    </section>

    <template v-if="report">
      <section class="report-summary" :class="`report-summary--${resultTone}`">
        <div><span>最终结果</span><strong>{{ resultText }}</strong></div>
        <div><span>{{ scoreCaption }}</span><strong>{{ report.total_score.toFixed(1) }}</strong></div>
        <div><span>证据覆盖率</span><strong>{{ (report.evidence_coverage * 100).toFixed(0) }}%</strong></div>
        <div v-if="analysisDurationMs != null"><span>分析用时</span><strong>{{ analysisDurationMs < 60000 ? `${(analysisDurationMs / 1000).toFixed(1)}s` : `${(analysisDurationMs / 60000).toFixed(1)}min` }}</strong></div>
        <p v-if="reportRejected || report.result === 'inconclusive'">{{ reportRejectReason || "由于关键证据不足，本次分数仅供参考，不判定为通过。" }}</p>
      </section>

      <section class="panel evidence-panel">
        <div class="section-label">{{ demoReplay ? "工程事件回放" : "步骤证据回放" }}</div>
        <EvidencePlayer :video-src="videoSource" :steps="stepJudgements" :active-step-index="activeEvidenceStep" @select="activeEvidenceStep = $event.step_index" />
      </section>

      <div class="report-grid">
        <section class="panel">
          <div class="section-label">步骤判断</div>
          <button v-for="step in stepJudgements" :key="step.step_id" type="button" class="judgement-row" @click="activeEvidenceStep = step.step_index">
            <span class="number">{{ step.step_index }}</span>
            <span class="judgement" :class="`judgement--${evidenceTone(step.judgement)}`">{{ resultLabel(step.judgement) }}</span>
            <span class="reason-stack">
              <span class="reason">{{ step.reason }}</span>
              <span v-if="observableCheckEntries(step).length" class="check-results" aria-label="逐项可观察检查">
                <span v-for="[checkText, checkStatus] in observableCheckEntries(step)" :key="checkText" class="check-result" :class="`check-result--${checkStatus}`">
                  <Check v-if="checkStatus === 'passed'" :size="12" />
                  <AlertTriangle v-else-if="checkStatus === 'failed'" :size="12" />
                  <CircleHelp v-else :size="12" />
                  {{ checkText }} · {{ observableCheckLabels[checkStatus] }}
                </span>
              </span>
            </span>
            <span class="time">{{ (step.start_ms / 1000).toFixed(1) }}s</span>
          </button>
        </section>
        <section class="panel">
          <div class="section-label">六维度得分</div>
          <div v-for="dimension in dimRows" :key="dimension.key" class="dimension-row"><span>{{ dimension.label }}</span><div><i :style="{ width: `${Math.max(0, Math.min(100, dimension.value))}%` }"></i></div><strong>{{ dimension.value.toFixed(1) }}</strong></div>
          <div class="model-note">规则 {{ report.scoring_rules_version }} · 事实源 {{ report.model_version }}<template v-if="demoReplay"> · fixture_replay</template></div>
          <div v-if="providerMetrics" class="provider-usage">
            Qwen {{ providerMetrics.calls }} 次调用 · {{ providerMetrics.total_tokens }} token · {{ (providerMetrics.latency_ms / 1000).toFixed(1) }} 秒
          </div>
        </section>
      </div>

      <section class="panel recommendations">
        <div class="section-label">
          教学反馈
          <span v-if="coaching" class="coaching-source">
            {{ coaching.source === "external_model" ? "大模型解释" : coaching.source === "external_model_partial" ? "大模型部分可用" : "规则建议" }}
          </span>
        </div>
        <template v-if="coaching">
          <p class="coaching-explanation">{{ coaching.explanation }}</p>
          <div class="practice-plan">
            <strong>复练计划</strong>
            <span v-if="coaching.practice_plan.focus_steps.length">重点步骤：{{ coaching.practice_plan.focus_steps.join("、") }}</span>
            <span>建议重复：{{ coaching.practice_plan.repeat_count }} 次</span>
            <span>{{ coaching.practice_plan.note }}</span>
          </div>
          <small class="score-guardrail">数值评分由固定规则产生；大模型只解释既有判定，不参与改分。</small>
        </template>
        <ol v-else><li v-for="item in recommendations" :key="item">{{ item }}</li></ol>
      </section>
    </template>

    <section class="history-section">
      <button type="button" class="history-toggle" @click="historyOpen = !historyOpen"><History :size="17" />历史提交（{{ sessions.length }}）<ChevronDown :size="16" :class="{ rotated: historyOpen }" /></button>
      <div v-if="historyOpen" class="history-list"><button v-for="session in sessions" :key="session.id" type="button" @click="openHistory(session)"><span><strong>{{ displayProcessName(session.process_key) }}</strong><small>{{ new Date(session.created_at).toLocaleString() }}</small></span><b>{{ session.status }}</b></button><p v-if="!sessions.length" class="empty">暂无历史提交</p></div>
    </section>
  </section>
</template>

<style scoped>
.page{max-width:1360px}.page-head{display:flex;gap:14px;align-items:flex-start;margin-bottom:18px}.head-led{width:9px;height:9px;margin-top:9px;border-radius:50%;background:var(--gx-accent);box-shadow:0 0 9px var(--gx-accent)}.page-head h1{margin:0 0 4px;font:700 26px var(--gx-font-display)}.page-head p{margin:0;color:var(--gx-text-muted);font-size:13px}.panel{background:linear-gradient(180deg,var(--gx-panel-hi),var(--gx-panel) 7%);border:1px solid var(--gx-panel-edge);border-radius:9px;box-shadow:inset 0 1px rgba(255,255,255,.06),0 2px 10px rgba(0,0,0,.32);padding:16px 18px}.demo-banner,.alert{display:flex;align-items:center;gap:8px;padding:11px 14px;margin:0 0 12px;border:1px solid;border-radius:7px;font-size:13px}.demo-banner{color:var(--gx-accent);border-color:rgba(255,181,71,.3);background:rgba(255,181,71,.08)}.alert--err{color:var(--gx-danger);border-color:rgba(255,92,92,.3);background:rgba(255,92,92,.08)}.alert--info{color:var(--gx-success);border-color:rgba(61,220,132,.28);background:rgba(61,220,132,.07)}.alert--warn{color:var(--gx-warning);border-color:rgba(255,181,71,.3);background:rgba(255,181,71,.08)}.wizard-steps{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;margin:0 0 14px;padding:0;list-style:none;border:1px solid var(--gx-panel-edge);border-radius:8px;overflow:hidden;background:var(--gx-panel-edge)}.wizard-steps li{min-height:64px;display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--gx-panel-lo);color:var(--gx-text-muted)}.wizard-steps li>span{width:30px;height:30px;display:grid;place-items:center;border:1px solid var(--gx-led-grey);border-radius:50%;font:700 12px var(--gx-font-mono)}.wizard-steps li div{display:flex;flex-direction:column;gap:2px}.wizard-steps small{font-size:11px}.wizard-steps li.active{color:var(--gx-text);background:rgba(255,181,71,.07)}.wizard-steps li.done>span{color:#14261d;border-color:var(--gx-led-green);background:var(--gx-led-green)}.submission-panel{display:grid;grid-template-columns:minmax(240px,.85fr) minmax(300px,1.2fr) minmax(210px,.65fr);gap:20px;margin-bottom:14px}.submission-column,.submission-action{min-width:0}.submission-action{padding-left:20px;border-left:1px solid var(--gx-panel-edge)}.section-label{margin-bottom:12px;color:var(--gx-text-muted);font:10px var(--gx-font-mono);letter-spacing:1.2px;text-transform:uppercase}.sop-option{min-height:58px;display:flex;align-items:center;gap:10px;padding:9px 10px;margin-bottom:7px;border:1px solid var(--gx-panel-edge);border-radius:7px;background:var(--gx-panel-lo);cursor:pointer}.sop-option.selected{border-color:var(--gx-accent);background:rgba(255,181,71,.08)}.sop-option>span{min-width:0;display:flex;flex:1;flex-direction:column}.sop-option small{color:var(--gx-text-muted);font-size:11px}.sop-option input{width:18px;height:18px;min-height:0}.segmented{display:grid;grid-template-columns:1fr 1fr;margin-bottom:10px}.segmented button{min-height:44px;display:flex;align-items:center;justify-content:center;gap:6px;border:1px solid var(--gx-panel-edge);color:var(--gx-text-muted);background:var(--gx-panel-lo);cursor:pointer}.segmented button:first-child{border-radius:7px 0 0 7px}.segmented button:last-child{border-radius:0 7px 7px 0}.segmented button.active{color:#291a00;border-color:var(--gx-accent);background:var(--gx-accent)}.drop-zone{min-height:126px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;padding:16px;border:1px dashed var(--gx-accent);border-radius:8px;text-align:center;background:rgba(255,181,71,.04);cursor:pointer}.drop-zone input{position:absolute;width:1px;height:1px;opacity:0}.drop-zone span{color:var(--gx-text-muted);font-size:12px}.asset-picker{display:flex;flex-direction:column;gap:6px}.asset-picker span{color:var(--gx-text-muted);font-size:11px}.asset-picker select{min-height:44px;width:100%}.shooting-tip{display:flex;flex-direction:column;gap:4px;padding:10px;margin-top:10px;border-left:2px solid var(--gx-accent);background:var(--gx-panel-lo);font-size:12px}.shooting-tip span{color:var(--gx-text-muted);line-height:1.5}.primary-action{width:100%;min-height:52px;display:flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--gx-accent-press);border-radius:7px;color:#291a00;background:linear-gradient(180deg,var(--gx-accent),var(--gx-accent-press));font-weight:700;cursor:pointer}.primary-action:disabled{opacity:.4;cursor:default}.submission-action small{display:block;margin-top:8px;color:var(--gx-text-muted);line-height:1.5}.progress-panel{display:flex;align-items:center;gap:14px;margin-bottom:14px}.progress-main{flex:1}.progress-main>div:first-child{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}.progress-main span{color:var(--gx-text-muted);font-size:12px}.track{height:8px;overflow:hidden;border-radius:4px;background:var(--gx-panel-lo)}.track span{display:block;height:100%;background:var(--gx-accent);transition:width 180ms ease}.spinner{color:var(--gx-accent);animation:spin 1s linear infinite}.retry{min-height:44px;display:flex;align-items:center;gap:6px;border:1px solid var(--gx-accent);border-radius:7px;color:var(--gx-accent);background:transparent;cursor:pointer}.retry:disabled{opacity:.5;cursor:default}.report-summary{display:grid;grid-template-columns:repeat(3,1fr);margin:16px 0;border:1px solid;border-radius:9px;overflow:hidden}.report-summary>div{min-height:92px;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:3px;background:var(--gx-panel-lo);border-right:1px solid var(--gx-panel-edge)}.report-summary span{color:var(--gx-text-muted);font-size:11px}.report-summary strong{font:700 30px var(--gx-font-display)}.report-summary p{grid-column:1/-1;margin:0;padding:10px 14px;text-align:center;font-size:12px;background:rgba(255,181,71,.08)}.report-summary--pass{border-color:rgba(61,220,132,.45)}.report-summary--pass strong{color:var(--gx-led-green)}.report-summary--fail{border-color:rgba(255,92,92,.45)}.report-summary--fail strong{color:var(--gx-led-red)}.report-summary--inconclusive{border-color:rgba(255,181,71,.45)}.report-summary--inconclusive strong{color:var(--gx-led-amber)}.evidence-panel{margin-bottom:14px}.report-grid{display:grid;grid-template-columns:1.2fr .8fr;gap:14px;margin-bottom:14px}.judgement-row{width:100%;min-height:54px;display:grid;grid-template-columns:34px 76px minmax(0,1fr) auto;gap:8px;align-items:center;padding:7px 8px;border:0;border-bottom:1px solid rgba(255,255,255,.05);color:var(--gx-text);text-align:left;background:transparent;cursor:pointer}.judgement-row:hover{background:rgba(255,181,71,.05)}.number{color:var(--gx-accent);font:700 17px var(--gx-font-display)}.judgement{font-size:11px}.judgement--pass{color:var(--gx-led-green)}.judgement--fail{color:var(--gx-led-red)}.judgement--inconclusive{color:var(--gx-led-amber)}.reason{font-size:12px}.time{color:var(--gx-text-muted);font:10px var(--gx-font-mono)}.dimension-row{display:grid;grid-template-columns:92px minmax(60px,1fr) 40px;gap:8px;align-items:center;min-height:38px;font-size:11px}.dimension-row>div{height:6px;overflow:hidden;border-radius:3px;background:var(--gx-panel-lo)}.dimension-row i{display:block;height:100%;background:var(--gx-accent)}.model-note{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.05);color:var(--gx-text-muted);font:10px var(--gx-font-mono)}.recommendations ol{margin:0;padding-left:24px}.recommendations li{padding:5px;line-height:1.5;font-size:13px}.history-section{margin-top:14px}.history-toggle{min-height:44px;display:flex;align-items:center;gap:8px;border:0;color:var(--gx-text-muted);background:transparent;cursor:pointer}.history-toggle svg:last-child{transition:transform 160ms ease}.history-toggle .rotated{transform:rotate(180deg)}.history-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px}.history-list button{min-height:58px;display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--gx-panel-edge);border-radius:7px;color:var(--gx-text);text-align:left;background:var(--gx-panel);cursor:pointer}.history-list button span{display:flex;flex-direction:column;gap:3px}.history-list small,.history-list b,.empty{color:var(--gx-text-muted);font-size:11px}.empty{padding:16px;text-align:center}@keyframes spin{to{transform:rotate(360deg)}}
.report-summary{grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.coaching-source{margin-left:auto;padding:3px 7px;border:1px solid rgba(255,181,71,.3);border-radius:4px;color:var(--gx-accent);font:10px var(--gx-font-mono);letter-spacing:0}.coaching-explanation{margin:0 0 12px;line-height:1.65;font-size:13px}.practice-plan{display:flex;flex-wrap:wrap;gap:8px 16px;padding:11px 12px;border-left:2px solid var(--gx-accent);background:var(--gx-panel-lo);font-size:12px}.practice-plan strong{color:var(--gx-accent)}.score-guardrail{display:block;margin-top:10px;color:var(--gx-text-muted);font-size:11px}
.replay-banner{align-items:flex-start;min-height:86px;padding:14px 16px;border-left:3px solid var(--gx-accent)}
.replay-banner>div{min-width:0;display:flex;flex:1;flex-direction:column;gap:7px}
.replay-banner>div>strong{color:var(--gx-text);font:700 16px var(--gx-font-display)}
.replay-banner p{margin:0;color:var(--gx-text-muted);line-height:1.55}
.replay-banner>a{min-height:44px;display:inline-flex;align-items:center;padding:8px 12px;border:1px solid var(--gx-accent);border-radius:7px;color:var(--gx-accent);text-decoration:none;white-space:nowrap}
.replay-meta{display:flex;flex-wrap:wrap;gap:6px}
.replay-meta b{padding:3px 7px;border:1px solid rgba(255,181,71,.3);border-radius:4px;color:var(--gx-accent);background:rgba(255,181,71,.08);font:10px var(--gx-font-mono)}
.provider-consent{min-height:62px;display:flex;align-items:flex-start;gap:10px;margin-top:9px;padding:9px 10px;border:1px solid var(--gx-panel-edge);border-radius:7px;background:var(--gx-panel-lo);cursor:pointer}
.provider-consent input{width:20px;height:20px;min-height:0;margin-top:1px;flex:0 0 auto}
.provider-consent>span{display:flex;flex-direction:column;gap:3px}
.provider-consent strong{font-size:12px}
.provider-consent small,.asset-picker small{color:var(--gx-text-muted);font-size:11px;line-height:1.45}
.asset-picker .consent-ok{color:var(--gx-success)}
.asset-picker .consent-warning{color:var(--gx-warning)}
.judgement-row{padding:9px 8px}
.reason-stack{min-width:0;display:flex;flex-direction:column;gap:7px}
.check-results{display:flex;flex-wrap:wrap;gap:5px}
.check-result{min-width:0;display:inline-flex;align-items:center;gap:4px;padding:3px 6px;border:1px solid var(--gx-panel-edge);border-radius:4px;color:var(--gx-text-muted);font-size:10px;line-height:1.35;overflow-wrap:anywhere}
.check-result--passed{color:var(--gx-led-green);border-color:rgba(61,220,132,.3)}
.check-result--failed{color:var(--gx-led-red);border-color:rgba(255,92,92,.3)}
.check-result--insufficient_evidence{color:var(--gx-led-amber);border-color:rgba(255,181,71,.3)}
@media(max-width:1000px){.submission-panel{grid-template-columns:1fr 1fr}.submission-action{grid-column:1/-1;padding:14px 0 0;border-left:0;border-top:1px solid var(--gx-panel-edge)}.report-grid{grid-template-columns:1fr}}@media(max-width:650px){.wizard-steps{grid-template-columns:1fr}.wizard-steps li{min-height:52px}.submission-panel{grid-template-columns:1fr}.report-summary{grid-template-columns:1fr}.report-summary>div{min-height:72px;border-right:0;border-bottom:1px solid var(--gx-panel-edge)}.judgement-row{grid-template-columns:28px 70px minmax(0,1fr)}.time{grid-column:3}.progress-panel{align-items:flex-start;flex-wrap:wrap}.progress-main{min-width:70%}}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
.provider-usage {
  margin-top: 6px;
  color: var(--gx-led-green);
  font: 10px var(--gx-font-mono);
}
</style>
