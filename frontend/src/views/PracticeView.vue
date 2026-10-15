<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { ArrowRight, BookOpen, Camera, CheckCircle2, Circle, Pause, Play, RefreshCw, Search, ShieldCheck, Sparkles, Video, XCircle } from "lucide-vue-next";
import { api, ApiError } from "@/api/client";
import type { CapturedFrame, LearningReport, PracticeCourse, PracticeEvent, PracticeSession } from "@/api/practice";
import { displayProcessName } from "@/services/displayNames";
import { useAuthStore } from "@/stores/auth";
import LearningCourseCard from "@/components/LearningCourseCard.vue";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const courses = ref<PracticeCourse[]>([]);
const catalogLoaded = ref(false);
const courseSearch = ref("");
const courseFilter = ref("all");
const history = ref<PracticeSession[]>([]);
const selectedCourse = ref("");
const consent = ref(false);
const session = ref<PracticeSession | null>(null);
const providerConfigured = ref(false);
const loading = ref(false);
const error = ref("");
const connected = ref(false);
const cameraReady = ref(false);
const cameraOpening = ref(false);
const automatic = ref(true);
const sending = ref(false);
const latencyMs = ref<number | null>(null);
const report = ref<LearningReport | null>(null);
const samplingFrames = ref(0);
const manualReason = ref("");
const cameraVideo = ref<HTMLVideoElement | null>(null);
const teachingVideo = ref<HTMLVideoElement | null>(null);
let media: MediaStream | null = null;
let events: EventSource | null = null;
let captureTimer: number | null = null;
let checkTimer: number | null = null;
let recoverTimer: number | null = null;
let sequence = 0;
let frames: CapturedFrame[] = [];
let serverOffset = 0;
let nextCheckAt = 0;
let disposed = false;
let loadGeneration = 0;
let cameraGeneration = 0;
const currentStep = computed(() => session.value?.steps.find(s => s.index === session.value?.state.current_step_index));
const course = computed(() => courses.value.find(c => c.sop_id === selectedCourse.value));
const courseTitle = (item: PracticeCourse) => item.course_meta?.title || displayProcessName(item.process_key);
const filteredCourses = computed(() => courses.value.filter(c => `${courseTitle(c)} ${c.course_meta?.description || ""}`.toLowerCase().includes(courseSearch.value.trim().toLowerCase()) && (courseFilter.value === "all" || (courseFilter.value === "ready" ? !c.issues.length : c.issues.length))));
function chooseCourse(id: string) { if (selectedCourse.value !== id) consent.value = false; selectedCourse.value = id; }
function courseProgress(id: string) { const item = history.value.find(h => h.sop_id === id); return item?.steps.length ? Math.round((item.state.completed.length + item.state.skipped.length) / item.steps.length * 100) : null; }
const progress = computed(() => session.value ? Math.round((session.value.state.completed.length + session.value.state.skipped.length) / session.value.steps.length * 100) : 0);
const writable = computed(() => auth.user?.role !== "observer");
const verificationPolicy = computed(() => currentStep.value?.verification?.confirm_windows === 1
  ? "多帧核验 · 通过即进入下一步"
  : "关键步骤 · 两轮画面确认");
const stateLabel = computed(() => {
  if (!session.value) return "准备练习";
  if (session.value.status === "paused") return "练习已暂停";
  if (session.value.status === "finished") return "课程已完成";
  if (session.value.status === "stopped") return "练习已结束";
  if (session.value.state.pending_observation_id || sending.value) return "正在核验";
  return { observing: "等待操作", confirming: "确认完成", correcting: "请修正操作", completed: "课程已完成" }[session.value.state.step_state];
});

function updateSession(incoming: PracticeSession) {
  if (session.value?.id === incoming.id && incoming.revision <= session.value.revision) return;
  const changed = incoming.id !== session.value?.id || incoming.state.step_revision !== session.value?.state.step_revision;
  session.value = incoming;
  serverOffset = incoming.server_time_ms - Date.now();
  sequence = Math.max(sequence, incoming.state.last_sequence);
  frames = changed ? [] : frames.filter(f => f.sequence > incoming.state.last_sequence);
  samplingFrames.value = frames.length;
  if (incoming.state.service_error) nextCheckAt = Date.now() + 10_000;
  if (incoming.state.unknown_streak) nextCheckAt = Date.now() + Math.min(15_000, 4000 * incoming.state.unknown_streak);
  if (incoming.status !== "active") stopCamera();
  if (changed) void nextTick(replayTeaching);
  if (incoming.status === "finished" && report.value === null) void loadReport(incoming.id);
}

async function loadReport(id: string) {
  try { report.value = await api.get<LearningReport>(`/practice-sessions/${id}/report`); }
  catch { report.value = null; }
}

async function loadCourses() {
  const [catalog, recent] = await Promise.all([
    api.get<{ courses: PracticeCourse[]; provider_configured: boolean }>("/practice-sessions/courses"),
    api.get<PracticeSession[]>("/practice-sessions"),
  ]);
  courses.value = catalog.courses;
  providerConfigured.value = catalog.provider_configured;
  history.value = recent;
  catalogLoaded.value = true;
  const requested = typeof route.query.course === "string" && catalog.courses.some(c => c.sop_id === route.query.course) ? route.query.course : "";
  selectedCourse.value ||= requested || catalog.courses.find(c => !c.issues.length)?.sop_id || catalog.courses[0]?.sop_id || "";
}

async function loadSession(id: string) {
  const generation = ++loadGeneration;
  events?.close();
  stopCamera();
  connected.value = false;
  loading.value = true;
  error.value = "";
  try {
    const incoming = await api.get<PracticeSession>(`/practice-sessions/${id}`);
    if (disposed || generation !== loadGeneration) return;
    session.value = null;
    report.value = null;
    updateSession(incoming);
    connectEvents(id);
  } catch (e) {
    if (generation === loadGeneration) error.value = e instanceof Error ? e.message : "练习加载失败";
  } finally {
    if (generation === loadGeneration) loading.value = false;
  }
}

function connectEvents(id: string) {
  events?.close();
  const stream = new EventSource(`/api/v1/practice-sessions/${id}/events`);
  events = stream;
  stream.onopen = () => { if (events === stream) connected.value = true; };
  stream.onerror = () => { if (events === stream) connected.value = false; };
  stream.onmessage = message => {
    if (disposed || events !== stream) return;
    try {
      const event = JSON.parse(message.data) as PracticeEvent;
      const isNew = !session.value || event.payload.session.revision > session.value.revision;
      if (isNew) {
        updateSession(event.payload.session);
        if (event.payload.result) latencyMs.value = event.payload.result.latency_ms;
      }
    } catch { error.value = "反馈连接异常，正在恢复进度。"; }
  };
}

async function startSession() {
  if (!selectedCourse.value || !consent.value || course.value?.issues.length) return;
  loading.value = true;
  error.value = "";
  try {
    const created = await api.post<PracticeSession>("/practice-sessions", { sop_id: selectedCourse.value, allow_provider_export: consent.value });
    await router.push({ name: "practice-session", params: { sessionId: created.id } });
  } catch (e) { error.value = e instanceof Error ? e.message : "无法开始练习"; }
  finally { loading.value = false; }
}

async function startCamera() {
  error.value = "";
  cameraOpening.value = true;
  const id = session.value?.id;
  const generation = ++cameraGeneration;
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("摄像头需要通过 localhost 或 HTTPS 打开页面。");
    const stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 960 }, height: { ideal: 540 } }, audio: false });
    if (disposed || generation !== cameraGeneration || session.value?.id !== id || session.value?.status !== "active") { stream.getTracks().forEach(t => t.stop()); return; }
    media = stream;
    await nextTick();
    if (generation !== cameraGeneration || disposed) { stream.getTracks().forEach(t => t.stop()); return; }
    if (cameraVideo.value) {
      cameraVideo.value.srcObject = stream;
      await cameraVideo.value.play();
    }
    if (generation !== cameraGeneration || disposed) return;
    cameraReady.value = true;
    samplingFrames.value = 0;
    captureTimer = window.setInterval(capture, 500);
  } catch (e) {
    if (generation !== cameraGeneration) return;
    stopCamera();
    const cameraErrors: Record<string, string> = {
      NotFoundError: "未检测到摄像头，请连接摄像头后重新开启。",
      NotAllowedError: "请允许浏览器使用摄像头后重新开启。",
      NotReadableError: "摄像头无法读取，请检查是否被其他应用占用。",
    };
    error.value = e instanceof Error ? cameraErrors[e.name] || `摄像头未开启：${e.message}` : "无法开启摄像头";
  } finally { if (generation === cameraGeneration) cameraOpening.value = false; }
}

function stopCamera() {
  ++cameraGeneration;
  cameraOpening.value = false;
  if (captureTimer !== null) window.clearInterval(captureTimer);
  captureTimer = null;
  media?.getTracks().forEach(track => track.stop());
  media = null;
  if (cameraVideo.value) cameraVideo.value.srcObject = null;
  cameraReady.value = false;
  frames = [];
  samplingFrames.value = 0;
}

function capture() {
  const video = cameraVideo.value;
  if (!video?.videoWidth || session.value?.status !== "active" || document.hidden) return;
  const canvas = document.createElement("canvas");
  canvas.width = Math.min(640, video.videoWidth);
  canvas.height = Math.round(canvas.width * video.videoHeight / video.videoWidth);
  canvas.getContext("2d")?.drawImage(video, 0, 0, canvas.width, canvas.height);
  frames.push({ sequence: ++sequence, captured_at_ms: Date.now() + serverOffset, data_uri: canvas.toDataURL("image/jpeg", 0.8) });
  frames = frames.slice(-6);
  samplingFrames.value = frames.length;
}

async function restartCourse() {
  if (!session.value || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    const created = await api.post<PracticeSession>("/practice-sessions", { sop_id: session.value.sop_id, allow_provider_export: true });
    await router.push({ name: "practice-session", params: { sessionId: created.id } });
  } catch (reason) { error.value = reason instanceof Error ? reason.message : "无法重新练习"; }
  finally { loading.value = false; }
}

async function checkStep(manual = false) {
  const active = session.value;
  if (!active || active.status !== "active" || !cameraReady.value || sending.value || active.state.pending_observation_id || !connected.value || !providerConfigured.value) return;
  if (!manual && (!automatic.value || Date.now() < nextCheckAt)) return;
  const available = frames.filter(f => f.sequence > active.state.last_sequence && f.captured_at_ms >= active.state.entered_at_ms).slice(-6);
  const chosen = available.length >= 5
    ? [available[0], available[Math.floor((available.length - 1) / 2)], available[available.length - 1]]
    : available;
  const spanMs = chosen.length ? chosen[chosen.length - 1].captured_at_ms - chosen[0].captured_at_ms : 0;
  if (chosen.length < 3 || spanMs < 1000) { if (manual) error.value = "正在采集多帧画面，请保持结果可见约一秒。"; return; }
  sending.value = true;
  error.value = "";
  try {
    await api.post(`/practice-sessions/${active.id}/observations`, { request_key: crypto.randomUUID(), step_revision: active.state.step_revision, frames: chosen });
    if (session.value?.id === active.id) updateSession(await api.get<PracticeSession>(`/practice-sessions/${active.id}`));
    nextCheckAt = Date.now() + 2500;
  } catch (e) {
    nextCheckAt = Date.now() + 10_000;
    if (e instanceof ApiError && e.status === 409) {
      try { if (session.value?.id === active.id) updateSession(await api.get<PracticeSession>(`/practice-sessions/${active.id}`)); } catch { /* recovery timer will reconnect */ }
    } else error.value = e instanceof Error ? e.message : "本轮检查失败，进度已保留。";
  } finally { sending.value = false; }
}

async function act(action: "pause" | "resume" | "stop" | "skip_optional") {
  if (!session.value || !writable.value) return;
  error.value = "";
  loading.value = true;
  try {
    const updated = await api.post<PracticeSession>(`/practice-sessions/${session.value.id}/actions`, { action });
    updateSession(updated);
  } catch (e) { error.value = e instanceof Error ? e.message : "操作失败"; }
  finally { loading.value = false; }
}

async function confirmManual(checkId: string) {
  if (!session.value || !auth.isWriter || !manualReason.value.trim()) return;
  try {
    updateSession(await api.post<PracticeSession>(`/practice-sessions/${session.value.id}/actions`, { action: "confirm_manual", check_ids: [checkId], reason: manualReason.value.trim() }));
    manualReason.value = "";
  } catch (e) { error.value = e instanceof Error ? e.message : "人工确认失败"; }
}

function replayTeaching() {
  const video = teachingVideo.value;
  const teaching = currentStep.value?.teaching;
  if (!video || !teaching) return;
  video.currentTime = teaching.start_ms / 1000;
  void video.play().catch(() => { /* user can start playback with native controls */ });
}

function constrainTeaching() {
  const video = teachingVideo.value;
  const teaching = currentStep.value?.teaching;
  if (video && teaching && video.currentTime * 1000 >= teaching.end_ms) video.pause();
}

function constrainPlay() {
  const video = teachingVideo.value;
  const teaching = currentStep.value?.teaching;
  if (video && teaching && (video.currentTime * 1000 < teaching.start_ms || video.currentTime * 1000 >= teaching.end_ms)) video.currentTime = teaching.start_ms / 1000;
}

function checkStatus(id: string, mode: string) {
  if (mode === "manual") return session.value?.state.manual_checks.includes(id) ? "satisfied" : "manual";
  return session.value?.state.checks.find(c => c.check_id === id)?.status ?? "unknown";
}

const labels: Record<string, string> = { satisfied: "已满足", not_yet: "进行中", violated: "需修正", unknown: "待观察", manual: "待教师确认" };

function checkReason(id: string) {
  const check = session.value?.state.checks.find(c => c.check_id === id);
  return check && ["violated", "unknown"].includes(check.status) ? check.reason : "";
}

function onVisibility() {
  if (document.hidden && session.value?.status === "active" && cameraReady.value) void act("pause");
}

watch(() => route.params.sessionId, id => {
  if (typeof id === "string") void loadSession(id);
  else { ++loadGeneration; events?.close(); events = null; stopCamera(); session.value = null; void loadCourses().catch(e => error.value = String(e)); }
});

onMounted(async () => {
  try {
    await loadCourses();
    if (typeof route.params.sessionId === "string") await loadSession(route.params.sessionId);
  } catch (e) { error.value = e instanceof Error ? e.message : "课程加载失败"; }
  checkTimer = window.setInterval(() => void checkStep(), 2500);
  recoverTimer = window.setInterval(async () => {
    const id = session.value?.id;
    if (!id || disposed || sending.value) return;
    try { const incoming = await api.get<PracticeSession>(`/practice-sessions/${id}`); if (!disposed && session.value?.id === id) updateSession(incoming); }
    catch { connected.value = false; }
  }, 5000);
  document.addEventListener("visibilitychange", onVisibility);
});

onBeforeUnmount(() => {
  disposed = true;
  ++loadGeneration;
  events?.close();
  stopCamera();
  if (checkTimer !== null) window.clearInterval(checkTimer);
  if (recoverTimer !== null) window.clearInterval(recoverTimer);
  document.removeEventListener("visibilitychange", onVisibility);
});
</script>

<template>
  <div class="practice-page">
    <header class="practice-head">
      <div><div class="eyebrow">LEARN · PRACTICE · IMPROVE</div><h1>{{ session ? '实训课堂' : '课程与 AI 陪练' }}</h1><p>看一步，练一步。完成后继续，出错时得到具体指导。</p></div>
      <span class="connection" :class="{ online: connected || session?.status === 'finished' }">{{ session?.status === 'finished' ? '报告已生成' : session ? (connected ? '反馈已连接' : '正在恢复连接') : '课程准备' }}</span>
    </header>
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <div v-if="catalogLoaded && !providerConfigured" class="notice">AI 陪练尚未就绪。可以浏览课程与示范；线下演示时可打开下方标有“工程回放”的既有报告，回放不代表实时模型识别。</div>

    <template v-if="!session">
      <div class="catalog-toolbar"><div class="catalog-tabs" aria-label="筛选课程"><button v-for="item in [{id:'all',label:'全部课程'},{id:'ready',label:'可以练习'},{id:'pending',label:'待教师准备'}]" :key="item.id" :class="{ active: courseFilter === item.id }" :aria-pressed="courseFilter === item.id" @click="courseFilter = item.id">{{ item.label }}</button></div><label class="catalog-search"><Search :size="16" /><input v-model="courseSearch" aria-label="搜索课程" placeholder="搜索你想学习的技能" /></label></div>
      <div class="catalog-layout"><section class="catalog-main"><div class="catalog-grid"><LearningCourseCard v-for="c in filteredCourses" :key="c.sop_id" :course="{title:courseTitle(c),lessons:c.step_count,version:c.version,ready:!c.issues.length,progress:courseProgress(c.sop_id)}" selectable :selected="selectedCourse === c.sop_id" @select="chooseCourse(c.sop_id)" /></div><div v-if="!catalogLoaded && !error" class="catalog-empty">正在加载教学课程…</div><div v-else-if="!filteredCourses.length" class="catalog-empty"><BookOpen :size="32" /><h2>{{ courses.length ? '没有找到匹配的课程' : '你的第一门技能课程，即将从这里开始' }}</h2><p>{{ courses.length ? '试试其他关键词，或者查看全部课程。' : '教师发布标准示范和学习步骤后，就可以开启动手练习。' }}</p><RouterLink v-if="auth.isWriter && !courses.length" to="/sops" class="gx-btn">准备教学课程<ArrowRight :size="15" /></RouterLink></div></section>
      <aside class="catalog-aside"><section class="gx-panel setup"><div class="panel-title"><Sparkles :size="19" /><h2>准备开始练习</h2></div><template v-if="course"><span class="setup-label">已选择的课程</span><h3 class="selected-title">{{ courseTitle(course) }}</h3><p>{{ course.step_count }} 个步骤 · 课程版本 v{{ course.version }}</p></template><p v-else>选择左侧的课程，开始本次技能探索。</p><label class="field">切换课程<select v-model="selectedCourse" @change="consent = false"><option value="" disabled>请选择课程</option><option v-for="c in courses" :key="c.sop_id" :value="c.sop_id">{{ courseTitle(c) }} · v{{ c.version }}</option></select></label><ul v-if="course?.issues.length" class="course-issues"><li v-for="issue in course.issues" :key="issue">{{ issue }}</li></ul><label class="consent"><input v-model="consent" type="checkbox" />我允许将本次练习的工作区画面发送给视觉模型，用于步骤核验。</label><button class="gx-btn gx-btn-primary start-course" :disabled="!writable || !consent || !course || !!course.issues.length || loading" @click="startSession"><Play :size="17" />{{ loading ? '正在准备…' : '开始课程' }}<ArrowRight :size="15" /></button><div class="setup-foot"><ShieldCheck :size="13" />开启摄像头前，先确认工作区画面授权。</div></section><section class="practice-how"><h3>让每一步都有反馈</h3><div v-for="(item,index) in [{title:'看清示范',text:'先理解当前步骤的操作与完成状态。'},{title:'动手练习',text:'将摄像头对准工作区，让关键细节完整入镜。'},{title:'获得指导',text:'完成后自动继续，放错时按提示修正。'}]" :key="item.title"><span>{{ index + 1 }}</span><p><strong>{{ item.title }}</strong><small>{{ item.text }}</small></p></div></section></aside></div>
      <section v-if="history.length" class="gx-panel recent"><div class="panel-title"><BookOpen :size="18" /><h2>最近的练习</h2></div><button v-for="item in history" :key="item.id" class="history-row" @click="router.push({ name: 'practice-session', params: { sessionId: item.id } })"><span>{{ item.course?.course_meta?.title || item.steps[0]?.name }} · {{ item.learner?.display_name || '我的练习' }}<small v-if="!providerConfigured && item.status === 'finished'">工程回放</small></span><span>{{ item.state.completed.length }}/{{ item.steps.length }} · {{ { active: '继续练习', paused: '已暂停', finished: '查看总结', stopped: '已结束' }[item.status] }}</span></button></section>
    </template>

    <template v-else>
      <div class="session-bar gx-panel"><span>{{ stateLabel }}</span><div class="progress"><span :style="{ width: `${progress}%` }"></span></div><strong>{{ progress }}%</strong><span class="metrics">{{ session.state.call_count }} 次核验 · {{ session.state.error_count }} 次纠错<span v-if="latencyMs !== null"> · {{ (latencyMs / 1000).toFixed(1) }}s</span></span></div>
      <div class="practice-grid">
        <aside class="gx-panel steps"><h2>学习路径</h2><div v-for="step in session.steps" :key="step.id" class="step-item" :class="{ current: currentStep?.index === step.index, done: session.state.completed.includes(step.index) }"><CheckCircle2 v-if="session.state.completed.includes(step.index)" :size="19" /><Circle v-else :size="19" /><div><small>STEP {{ String(step.index).padStart(2, '0') }}{{ step.optional ? ' · 可选' : '' }}</small><strong>{{ step.name }}</strong><span v-if="session.state.skipped.includes(step.index)">已跳过可选步骤</span></div></div></aside>
        <section class="gx-panel workspace">
          <div class="panel-title"><Camera :size="20" /><h2>你的操作画面</h2><span class="policy-chip">{{ verificationPolicy }}</span><span v-if="cameraReady" class="live-dot">LIVE</span></div>
          <div class="camera-stage"><video ref="cameraVideo" autoplay muted playsinline></video><div v-if="!cameraReady" class="camera-empty"><Camera :size="38" /><strong>{{ session.status === 'active' ? '将摄像头对准工作区' : '摄像头已停止' }}</strong><p>完整展示零件与操作结果，完成后自然移开手。</p></div><span v-if="session.state.pending_observation_id" class="checking">正在检查当前步骤…</span><span v-else-if="cameraReady && samplingFrames < 3" class="sampling">保持结果清晰可见 · 正在采集 {{ samplingFrames }}/3</span><span v-else-if="cameraReady && session.state.unknown_streak" class="sampling">画面不清，{{ Math.min(15, 4 * session.state.unknown_streak) }} 秒后自动补拍</span></div>
          <div class="camera-controls"><button v-if="!cameraReady && session.status === 'active'" class="gx-btn gx-btn-primary" :disabled="cameraOpening || !writable" @click="startCamera"><Camera :size="16" />{{ cameraOpening ? '正在开启…' : '开启摄像头' }}</button><button v-if="cameraReady" class="gx-btn" @click="stopCamera">关闭摄像头</button><label><input v-model="automatic" type="checkbox" />自动检查</label><button class="gx-btn" :disabled="!cameraReady || !providerConfigured || sending || !!session.state.pending_observation_id || !connected" @click="checkStep(true)"><RefreshCw :size="16" />检查这一步</button></div>
          <div class="feedback" :class="session.state.step_state" aria-live="polite"><small>{{ session.state.service_error ? '连接提示' : '陪练提示' }}</small><strong>{{ session.state.service_error || session.state.feedback }}</strong></div>
          <div v-if="currentStep" class="condition-list"><h3>本步骤完成条件</h3><div v-for="check in currentStep.verification?.checks" :key="check.id" class="condition" :class="checkStatus(check.id, check.mode)"><CheckCircle2 v-if="checkStatus(check.id, check.mode) === 'satisfied'" :size="17" /><XCircle v-else-if="checkStatus(check.id, check.mode) === 'violated'" :size="17" /><Circle v-else :size="17" /><span>{{ check.criterion }}<small v-if="!check.required">（建议）</small><small v-if="checkReason(check.id)" class="condition-reason">{{ checkReason(check.id) }}</small></span><em>{{ labels[checkStatus(check.id, check.mode)] }}</em></div></div>
          <div v-if="auth.isWriter && currentStep?.verification?.checks.some(c => c.mode === 'manual')" class="manual-review"><label>人工核验依据<input v-model="manualReason" placeholder="记录现场检查的方法与结果" /></label><button v-for="check in currentStep.verification.checks.filter(c => c.mode === 'manual')" :key="check.id" class="gx-btn" :disabled="!manualReason.trim() || session.state.manual_checks.includes(check.id)" @click="confirmManual(check.id)">教师确认：{{ check.criterion }}</button></div>
          <div v-if="session.status === 'finished'" class="summary"><CheckCircle2 :size="36" /><h2>完成本次课程</h2><div v-if="report" class="report-metrics"><span><small>本次练习指数</small><b>{{ report.practice_index }}</b></span><span><small>完成率</small><b>{{ report.completion_rate }}%</b></span><span><small>纠错</small><b>{{ report.correction_count }} 次</b></span><span><small>核验调用</small><b>{{ report.call_count }} 次</b></span></div><p>{{ report?.method_note || `已完成 ${session.state.completed.length} 步，纠正 ${session.state.error_count} 次。` }}</p><h3 v-if="report?.weak_steps.length">建议重点复习</h3><div v-for="item in report?.steps || []" :key="item.step_index" :class="{ 'needs-review': item.needs_review }">{{ item.name }}<strong>{{ Math.round(item.duration_ms / 1000) }} 秒 · {{ item.error_count }} 次纠错<span v-if="item.needs_review"> · 建议复习</span></strong></div><button class="gx-btn gx-btn-primary repractice" @click="restartCourse"><RefreshCw :size="16" />重新练习本课程</button></div>
        </section>
        <aside class="gx-panel teaching"><div class="panel-title"><Video :size="19" /><h2>当前步骤示范</h2></div><template v-if="currentStep"><div class="step-number">{{ String(currentStep.index).padStart(2, '0') }}</div><h3>{{ currentStep.name }}</h3><p>{{ currentStep.teaching?.instruction || '对照示范视频完成当前步骤。' }}</p><video ref="teachingVideo" :src="`/api/v1/practice-sessions/${session.id}/teaching-video`" controls playsinline preload="metadata" @loadedmetadata="replayTeaching" @timeupdate="constrainTeaching" @play="constrainPlay"></video><button class="gx-btn replay" @click="replayTeaching"><Play :size="16" />重播本步骤</button><small>完成后的标准状态</small><img :key="currentStep.index" :src="`/api/v1/practice-sessions/${session.id}/reference?step_index=${currentStep.index}`" alt="当前步骤完成后的标准参考图" /><p class="teaching-note">看不清关键细节时，调整视角再检查；需要人工核验的条件由教师确认。</p></template><p v-else>本次练习已完成。你可以返回课程，开始下一次练习。</p></aside>
      </div>
      <footer class="session-actions"><button v-if="session.status === 'active'" class="gx-btn" :disabled="loading" @click="act('pause')"><Pause :size="16" />暂停练习</button><button v-if="session.status === 'paused'" class="gx-btn gx-btn-primary" :disabled="loading" @click="act('resume')"><Play :size="16" />继续练习</button><button v-if="currentStep?.optional && session.status === 'active'" class="gx-btn" @click="act('skip_optional')">跳过可选步骤</button><button v-if="['active', 'paused'].includes(session.status)" class="gx-btn" :disabled="loading" @click="act('stop')">结束练习</button><button class="gx-btn" @click="router.push({ name: 'practice' })">返回课程</button></footer>
    </template>
  </div>
</template>

<style scoped>
.condition-reason{display:block;font-size:11px;line-height:1.7;margin-top:4px;opacity:.85}
.policy-chip{margin-left:auto;padding:4px 8px;border-radius:999px;background:var(--gx-accent-soft);color:var(--gx-accent);font-size:9px}.policy-chip+.live-dot{margin-left:6px}
.manual-review{display:grid;gap:10px;padding:14px;margin-top:16px;border:1px solid var(--gx-panel-edge);border-radius:6px}.manual-review label{display:grid;gap:8px;font-size:12px}.manual-review input{background:var(--gx-panel-lo);border:1px solid var(--gx-panel-edge);color:var(--gx-text);padding:10px;border-radius:4px}
.practice-page{padding:24px;max-width:1600px;margin:auto}.practice-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;gap:20px}.eyebrow{font-size:10px;letter-spacing:2px;color:var(--gx-accent)}h1{font-size:28px;margin:8px 0}h2{font-size:16px;margin:0}h3{font-size:14px}.practice-head p,.setup p{color:var(--gx-text-muted);font-size:13px}.connection{font-size:12px;border:1px solid var(--gx-panel-edge);padding:8px 12px;border-radius:20px;color:var(--gx-text-muted)}.connection.online{color:var(--gx-led-green);border-color:var(--gx-led-green)}.gx-panel{padding:18px}.notice{padding:14px 16px;border-left:3px solid var(--gx-accent);background:var(--gx-panel);margin-bottom:16px;font-size:13px;line-height:1.6}.notice.error{border-color:var(--gx-danger);color:var(--gx-danger)}.setup{max-width:760px}.panel-title{display:flex;align-items:center;gap:9px;margin-bottom:16px}.field{display:grid;gap:8px;margin:20px 0;color:var(--gx-text-muted);font-size:12px}.field select{padding:12px;background:var(--gx-panel-lo);color:var(--gx-text);border:1px solid var(--gx-panel-edge);border-radius:6px}.consent{display:flex;align-items:center;gap:10px;font-size:13px;line-height:1.6;margin:20px 0}.gx-btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:42px;padding:8px 14px;border:1px solid var(--gx-panel-edge);border-radius:6px;background:var(--gx-panel-lo);color:var(--gx-text);cursor:pointer}.gx-btn-primary{background:var(--gx-accent);color:#291a00;font-weight:700;border-color:var(--gx-accent)}.gx-btn:disabled{opacity:.4;cursor:default}.course-issues{color:var(--gx-accent);font-size:12px;line-height:1.8;padding-left:18px}.empty{padding:20px 0;font-size:13px;color:var(--gx-text-muted)}.recent{max-width:760px;margin-top:20px}.history-row{display:flex;justify-content:space-between;gap:14px;width:100%;padding:16px 0;background:transparent;border:0;border-bottom:1px solid var(--gx-panel-edge);color:var(--gx-text);text-align:left;cursor:pointer}.history-row span:last-child{color:var(--gx-accent);font-size:12px}.session-bar{display:flex;align-items:center;gap:14px;margin-bottom:18px;font-size:13px}.session-bar>span:first-child{min-width:90px;color:var(--gx-accent)}.progress{height:6px;max-width:280px;flex:1;background:var(--gx-panel-lo);border-radius:5px;overflow:hidden}.progress span{display:block;height:100%;background:var(--gx-accent);transition:width .3s}.metrics{margin-left:auto;color:var(--gx-text-muted);font-size:11px}.practice-grid{display:grid;grid-template-columns:190px minmax(280px,1fr) 280px;gap:16px;align-items:start}.steps{padding:18px 12px}.steps h2{padding:0 8px;margin-bottom:18px}.step-item{display:flex;align-items:flex-start;gap:10px;padding:14px 9px;border-radius:7px;margin:5px 0;color:var(--gx-text-muted)}.step-item.current{color:var(--gx-accent);background:rgba(255,181,71,.09);box-shadow:inset 2px 0 var(--gx-accent)}.step-item.done{color:var(--gx-led-green)}.step-item div{display:grid;gap:6px}.step-item small{font-size:9px;letter-spacing:1px}.step-item strong{font-size:13px;line-height:1.5}.step-item span{font-size:10px}.workspace{min-width:0}.live-dot{font-size:10px;color:var(--gx-led-green);margin-left:auto}.camera-stage{position:relative;background:#0b1015;aspect-ratio:16/10;border-radius:8px;overflow:hidden;border:1px solid var(--gx-panel-edge)}.camera-stage video{width:100%;height:100%;object-fit:contain}.camera-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:14px;padding:16px;color:var(--gx-text-muted)}.camera-empty strong{font-size:15px}.camera-empty p{font-size:12px;max-width:280px;line-height:1.6}.checking{position:absolute;bottom:14px;left:14px;font-size:12px;color:var(--gx-accent);background:rgba(0,0,0,.8);border:1px solid var(--gx-accent);padding:8px 12px;border-radius:6px}.camera-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:14px}.camera-controls label{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--gx-text-muted)}.feedback{display:grid;gap:9px;line-height:1.6;padding:16px;margin-top:18px;border-left:3px solid var(--gx-accent);background:var(--gx-panel-lo)}.feedback small{color:var(--gx-text-muted);font-size:10px;letter-spacing:1px}.feedback strong{font-size:14px;font-weight:500}.feedback.correcting{border-color:var(--gx-danger)}.feedback.confirming{border-color:var(--gx-led-green)}.condition-list{margin-top:20px}.condition-list h3{color:var(--gx-text-muted);font-size:12px}.condition{display:flex;align-items:flex-start;gap:8px;margin:12px 0;font-size:12px;color:var(--gx-text-muted);line-height:1.6}.condition>svg{flex-shrink:0;margin-top:2px}.condition.satisfied{color:var(--gx-led-green)}.condition.violated{color:var(--gx-danger)}.condition em{white-space:nowrap;font-size:10px;font-style:normal;margin-left:auto}.teaching{min-width:0}.step-number{font-size:44px;line-height:1;color:var(--gx-accent);font-weight:700;margin-top:24px}.teaching h3{font-size:18px;margin:12px 0}.teaching p{font-size:12px;line-height:1.8;color:var(--gx-text-muted)}.teaching video,.teaching img{display:block;width:100%;border-radius:6px;background:#0b1015}.replay{width:100%;margin:10px 0 20px}.teaching>small{display:block;margin:12px 0 8px;color:var(--gx-text-muted);font-size:11px}.teaching-note{border-top:1px solid var(--gx-panel-edge);padding-top:12px}.session-actions{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}.summary{margin-top:22px;padding-top:20px;border-top:1px solid var(--gx-panel-edge);color:var(--gx-led-green)}.summary h2{margin:12px 0}.summary p{color:var(--gx-text-muted);font-size:13px;line-height:1.7}.summary>div{display:flex;justify-content:space-between;gap:12px;margin-top:12px;color:var(--gx-text);font-size:12px}.summary strong{color:var(--gx-text-muted)}
@media(max-width:1200px){.practice-grid{grid-template-columns:160px minmax(260px,1fr)}.teaching{grid-column:2}.metrics{display:none}}@media(max-width:760px){.practice-page{padding:14px}.practice-head{align-items:flex-start}.practice-head h1{font-size:24px}.connection{font-size:10px;white-space:nowrap}.practice-grid{grid-template-columns:1fr}.steps{display:none}.teaching{grid-column:1}.session-bar{gap:8px}.camera-controls{gap:7px}.camera-controls .gx-btn{font-size:12px}.history-row{font-size:12px}}
.practice-page{padding:0;max-width:none}.practice-head{margin-bottom:22px}.practice-head h1{font-size:26px;font-weight:700;letter-spacing:-.5px}.eyebrow{font-size:9px;color:#929aad;letter-spacing:1.5px}.practice-head p{font-size:12px;margin-top:5px}.connection{padding:6px 10px;font-size:10px;background:white}.connection.online{background:#eaf5ef;border-color:#c8e3d6}.notice{border-radius:9px;background:#f0edfc;color:#71678d;border-left-color:var(--gx-accent)}.notice.error{background:#fff3f5;color:var(--gx-danger)}.catalog-toolbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:22px}.catalog-tabs{display:flex;gap:5px;padding:4px;background:#eaeaf3;border-radius:9px}.catalog-tabs button{min-height:34px;padding:7px 13px;border:0;border-radius:7px;color:#777d90;background:transparent;font-size:11px}.catalog-tabs button.active{background:white;color:var(--gx-accent);box-shadow:0 2px 5px #20263b05}.catalog-search{display:flex;align-items:center;gap:8px;border:1px solid var(--gx-border);border-radius:9px;background:white;padding:0 12px;color:#a0a6b6}.catalog-search input{min-height:39px;width:215px;border:0;background:none;padding:7px 0;font-size:11px;box-shadow:none}.catalog-layout{display:grid;grid-template-columns:minmax(0,1fr) 285px;gap:23px;align-items:start}.catalog-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.catalog-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:315px;padding:30px;border:1px dashed #d4cfe7;background:white;border-radius:14px;color:#9690a9;gap:14px}.catalog-empty h2{font-size:15px;color:var(--gx-text)}.catalog-empty p{font-size:12px;line-height:1.9;max-width:360px;margin:0}.catalog-aside{min-width:0}.setup{padding:22px;max-width:none}.setup .panel-title{color:var(--gx-accent);margin-bottom:22px}.setup .panel-title h2{color:var(--gx-text);font-size:14px}.setup-label{font-size:10px;color:#969bad}.selected-title{font-size:15px;margin:8px 0;line-height:1.8;overflow-wrap:anywhere}.setup p{font-size:10px;line-height:1.9}.setup .field{font-size:10px;margin:20px 0 0}.setup .field select{width:100%;font-size:11px;background-color:white;min-height:41px;padding:9px}.consent{font-size:10px;align-items:flex-start;margin:20px 0;gap:8px;color:#81879a}.consent input{margin-top:4px}.start-course{width:100%}.start-course svg:last-child{margin-left:auto}.gx-btn-primary{background:var(--gx-accent);color:white;font-weight:600;border-radius:9px}.setup-foot{display:flex;align-items:flex-start;gap:5px;font-size:9px;color:#a0a5b5;margin-top:14px;line-height:1.8}.setup-foot svg{flex-shrink:0;margin-top:3px}.practice-how{padding:22px 7px 0}.practice-how h3{font-size:12px;margin:0 0 17px}.practice-how>div{display:flex;gap:12px;margin-bottom:14px}.practice-how>div>span{display:grid;place-items:center;width:25px;height:25px;border-radius:8px;flex-shrink:0;background:#eeebf9;color:#9c8cbc;font-size:10px}.practice-how p{margin:0;font-size:10px;line-height:1.7}.practice-how strong{display:block;font-weight:600}.practice-how small{display:block;color:#9198aa;font-size:9px;margin-top:3px}.recent{max-width:none;margin-top:24px;padding:22px}.recent h2{font-size:14px}.history-row{font-size:12px;padding:14px 0}.history-row:hover{color:var(--gx-accent)}.session-bar{padding:17px 20px;border-radius:12px}.practice-grid{grid-template-columns:175px minmax(280px,1fr) 260px;gap:18px}.steps{padding:20px 11px}.steps h2{font-size:13px}.step-item.current{background:var(--gx-accent-soft);box-shadow:none;border:1px solid #e0dbf7}.step-item{padding:12px 9px}.step-item small{font-size:8px}.step-item strong{font-size:11px}.panel-title h2{font-size:14px}.camera-stage{border-radius:11px;background:#1e2535}.camera-empty{color:#b2bdcf}.camera-empty strong{color:#e3e9f2}.camera-empty p{color:#9da8ba}.checking{background:#312a52dd;color:#e5dfff;border-color:#706a96}.camera-controls .gx-btn{font-size:11px}.feedback{border-radius:10px;background:#f3f1fb}.feedback.confirming{background:#eff7f2}.feedback.correcting{background:#fff3f5}.condition em{color:#6b7280}.teaching h3{font-size:16px}.step-number{font-size:36px;margin-top:18px}.teaching video,.teaching img{border-radius:8px}.metrics{font-size:10px}.session-actions .gx-btn{font-size:11px}
@media(min-width:1500px){.catalog-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1200px){.practice-grid{grid-template-columns:155px minmax(260px,1fr)}.teaching{grid-column:2}}
@media(max-width:760px){.practice-page{padding:0}.catalog-toolbar{align-items:stretch;flex-direction:column;gap:12px}.catalog-tabs{width:fit-content}.catalog-search input{width:100%}.catalog-layout{grid-template-columns:1fr;gap:20px}.catalog-grid{grid-template-columns:1fr 1fr;gap:12px}.catalog-aside{display:grid;grid-template-columns:1fr;gap:10px}.practice-head h1{font-size:23px}.practice-head{gap:12px}.practice-grid{grid-template-columns:1fr}.teaching{grid-column:1}.steps{display:flex;align-items:center;gap:8px;overflow-x:auto;padding:9px}.steps h2{display:none}.step-item{min-width:135px;flex-shrink:0;margin:0;padding:9px}.step-item strong{font-size:10px}.session-bar{padding:13px}.practice-head p{font-size:11px}.recent{padding:18px}.practice-how{padding:15px 7px 0}.catalog-empty{padding:25px}.connection{font-size:9px}}
@media(max-width:480px){.catalog-grid{grid-template-columns:1fr}.catalog-tabs button{padding:7px 10px;font-size:10px}.practice-head{flex-wrap:wrap}.practice-head .connection{margin-top:-10px}.camera-controls .gx-btn{font-size:10px}.session-bar>span:first-child{font-size:11px;min-width:70px}}
.sampling{position:absolute;bottom:14px;left:14px;padding:8px 12px;border:1px solid #8e82c8;border-radius:6px;background:rgba(0,0,0,.8);color:#e7e5ff;font-size:12px}.report-metrics{display:grid!important;grid-template-columns:repeat(4,1fr);gap:8px}.report-metrics span{display:grid;gap:4px;padding:10px;border-radius:8px;background:#eff7f2}.report-metrics small{font-size:9px;color:var(--gx-text-muted)}.report-metrics b{font-size:18px;color:var(--gx-success)}.summary>div.needs-review{color:#a66536;background:#fff7ee;padding:8px;border-radius:7px}.repractice{margin-top:18px}
</style>
