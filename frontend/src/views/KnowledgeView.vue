<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { Upload, RefreshCw, WandSparkles } from "lucide-vue-next";
import { RouterLink } from "vue-router";

import { api, ApiError } from "@/api/client";
import type { Asset, JobDetail } from "@/api/types";
import { isActiveJobStatus, pollingRetryDelayMs } from "@/services/jobPolling";
import { displayAssetName } from "@/services/displayNames";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();

const assets = ref<Asset[]>([]);
const kind = ref<"video" | "manual">("video");
const licenseNote = ref("");
const allowProviderExport = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const error = ref("");
const info = ref("");
const busy = ref(false);
const assetJobs = ref<Record<string, JobDetail>>({});
const processingRequests = ref<Record<string, boolean>>({});
const pollWarnings = ref<Record<string, string>>({});
const pollTimers = new Map<string, number>();

async function load() {
  assets.value = await api.get<Asset[]>("/assets");
}

function stopPolling(assetId: string) {
  const timer = pollTimers.get(assetId);
  if (timer !== undefined) window.clearTimeout(timer);
  pollTimers.delete(assetId);
}

function isProcessing(asset: Asset): boolean {
  if (processingRequests.value[asset.id]) return true;
  const status = assetJobs.value[asset.id]?.status || asset.processing_status;
  return isActiveJobStatus(status);
}

function setPollWarning(assetId: string, message: string) {
  const next = { ...pollWarnings.value };
  if (message) next[assetId] = message;
  else delete next[assetId];
  pollWarnings.value = next;
}

async function pollJob(assetId: string, jobId: string, consecutiveFailures = 0) {
  stopPolling(assetId);
  try {
    const job = await api.get<JobDetail>(`/jobs/${jobId}`);
    setPollWarning(assetId, "");
    assetJobs.value = { ...assetJobs.value, [assetId]: job };
    if (job.status === "succeeded") {
      info.value = `资产处理完成：${job.status_text}`;
      await load();
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      error.value = job.error_summary || "资产处理失败，可重新点击处理。";
      return;
    }
    pollTimers.set(assetId, window.setTimeout(() => void pollJob(assetId, jobId), 1000));
  } catch (reason) {
    if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403 || reason.status === 404)) {
      setPollWarning(assetId, "");
      error.value = reason.message;
      return;
    }
    const failures = consecutiveFailures + 1;
    const delay = pollingRetryDelayMs(failures);
    setPollWarning(assetId, `进度连接中断，${Math.round(delay / 1000)} 秒后自动重连（第 ${failures} 次）`);
    pollTimers.set(
      assetId,
      window.setTimeout(() => void pollJob(assetId, jobId, failures), delay),
    );
  }
}

async function onUpload() {
  const file = fileInput.value?.files?.[0];
  if (!file) {
    error.value = "请选择文件";
    return;
  }
  error.value = "";
  info.value = "";
  busy.value = true;
  try {
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind.value);
    form.append("license_note", licenseNote.value);
    form.append("allow_provider_export", String(allowProviderExport.value));
    const queued = await api.postForm<{
      asset: Asset;
      job_id: string;
      job_status: string;
    }>("/assets/upload-and-process", form);
    info.value = `已上传并加密 ${file.name}，处理任务已进入队列。`;
    if (fileInput.value) fileInput.value.value = "";
    licenseNote.value = "";
    allowProviderExport.value = false;
    await load();
    await pollJob(queued.asset.id, queued.job_id);
  } catch (e) {
    error.value = e instanceof Error ? e.message : "上传失败";
  } finally {
    busy.value = false;
  }
}

async function onProcess(asset: Asset) {
  if (processingRequests.value[asset.id]) return;
  error.value = "";
  info.value = "";
  processingRequests.value = { ...processingRequests.value, [asset.id]: true };
  try {
    const job = await api.post<{ job_id: string; status: string }>(
      `/assets/${asset.id}/process`,
    );
    info.value = `已入队处理任务 ${job.job_id.slice(0, 8)}，页面会自动更新。`;
    await pollJob(asset.id, job.job_id);
  } catch (e) {
    error.value = e instanceof Error ? e.message : "入队失败";
  } finally {
    processingRequests.value = { ...processingRequests.value, [asset.id]: false };
  }
}

onMounted(async () => {
  try {
    await load();
    for (const asset of assets.value) {
      if (asset.processing_job_id && isProcessing(asset)) {
        void pollJob(asset.id, asset.processing_job_id);
      }
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "资产列表加载失败";
  }
});
onBeforeUnmount(() => {
  for (const assetId of pollTimers.keys()) stopPolling(assetId);
});
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div class="head-led" aria-hidden="true"></div>
      <div>
        <h1>教学素材</h1>
        <p class="muted">导入标准教学视频与参考资料，为下一门技能课程准备示范。</p>
      </div>
    </header>

    <p v-if="error" class="alert alert--err"><span class="alert-led"></span>{{ error }}</p>
    <p v-if="info" class="alert alert--info"><span class="alert-led"></span>{{ info }}</p>

    <!-- 投料口：上传区 -->
    <form class="panel feed-panel" @submit.prevent="onUpload">
      <div class="panel-label"><Upload :size="12" /> 上传教学素材</div>

      <div class="feed-grid">
        <label class="field">
          <span class="field-label">素材类型</span>
          <select v-model="kind" class="gx-select">
            <option value="manual">手册 / 规范（PDF / DOCX / TXT）</option>
            <option value="video">标准教学视频（MP4）</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">授权 / 来源说明</span>
          <input
            v-model="licenseNote"
            type="text"
            class="gx-input"
            placeholder="如：公司内部授权、采购教材"
          />
        </label>
      </div>

      <label class="export-check">
        <input v-model="allowProviderExport" type="checkbox" class="gx-check" />
        <span class="check-label">
          允许将本资产的关键帧/文本发送给外部 AI 分析（SOP 编译需要；未勾选则不可用于编译）
        </span>
      </label>

      <div class="feed-slot">
        <input
          ref="fileInput"
          type="file"
          class="gx-file"
          :accept="kind === 'video' ? 'video/*' : '.pdf,.docx,.doc,.txt,.md'"
        />
        <button type="submit" class="gx-btn gx-btn--primary" :disabled="busy">
          <Upload :size="16" />
          {{ busy ? "上传中…" : "上传素材" }}
        </button>
      </div>
    </form>

    <!-- 资产清单：文件柜抽屉 -->
    <div class="panel vault-panel">
      <div class="panel-label">已导入的教学素材（{{ assets.length }}）</div>

      <div v-if="assets.length" class="drawers">
        <div v-for="a in assets" :key="a.id" class="drawer">
          <div
            class="drawer-led"
            :class="a.has_encrypted_storage ? 'led-green' : 'led-amber'"
            aria-hidden="true"
          ></div>
          <div class="drawer-body">
            <div class="drawer-top">
              <span class="drawer-name">{{ displayAssetName(a.filename) }}</span>
              <span class="kind-tag" :class="`kind--${a.kind}`">{{ {video:"教学视频",manual:"参考资料",image:"图片",sensor:"传感器资料"}[a.kind] || a.kind }}</span>
            </div>
            <div class="drawer-meta">
              <span class="muted">授权 {{ a.license_note || "—" }}</span>
              <span class="meta-sep">·</span>
              <span class="ai-flag" :class="a.allow_provider_export ? 'ai-on' : 'ai-off'">
                <span class="ai-dot"></span>外部AI {{ a.allow_provider_export ? "已授权" : "未授权" }}
              </span>
              <span v-if="a.kind !== 'video' && a.indexed" class="process-state process-state--done">参考资料已就绪</span>
              <span v-else-if="isProcessing(a)" class="process-state">{{ assetJobs[a.id]?.status_text || "正在处理" }} · {{ assetJobs[a.id]?.progress_percent || 0 }}%</span>
              <span v-else-if="a.processing_status === 'succeeded'" class="process-state process-state--done">{{ a.kind === "video" ? "视频预览已准备好" : "处理完成（无可索引文本）" }}</span>
              <span v-else-if="a.processing_status === 'failed'" class="process-state process-state--failed">处理失败</span>
              <span v-if="pollWarnings[a.id]" class="process-state process-state--warning">{{ pollWarnings[a.id] }}</span>
            </div>
          </div>
          <button
            class="gx-btn gx-btn--ghost drawer-btn"
            @click="onProcess(a)"
            :disabled="!auth.isWriter || isProcessing(a)"
          >
            <RefreshCw :size="14" :class="{ spinning: isProcessing(a) }" /> {{ isProcessing(a) ? "处理中" : "处理" }}
          </button>
          <RouterLink
            v-if="a.kind === 'video' && auth.isWriter && a.allow_provider_export"
            class="gx-btn gx-btn--primary drawer-btn"
            :to="{ name: 'sop-studio', query: { asset: a.id } }"
          >
            <WandSparkles :size="14" /> 编排课程
          </RouterLink>
          <span v-else-if="a.kind === 'video' && auth.isWriter" class="gx-btn drawer-btn disabled-action" title="重新上传并勾选外部 AI 授权后可生成 SOP">未授权生成 SOP</span>
        </div>
      </div>

      <p v-else class="muted empty">还没有教学素材，先上传一段标准示范吧。</p>
    </div>
  </section>
</template>

<style scoped>
.page {
  max-width: 1100px;
}

/* 页头 */
.page-head {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 20px;
}
.head-led {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: radial-gradient(circle at 30% 30%, #ffd089, var(--gx-accent) 60%, #b87a1f);
  box-shadow: 0 0 10px var(--gx-accent);
  margin-top: 8px;
  flex-shrink: 0;
}
.page-head h1 {
  font-family: var(--gx-font-display);
  font-size: 26px;
  margin: 0 0 4px;
  color: var(--gx-text);
  letter-spacing: 0.5px;
}
.muted {
  color: var(--gx-text-muted);
  font-size: 13px;
}
.small {
  font-size: 12px;
}
.mono {
  font-family: var(--gx-font-mono);
  font-size: 12px;
  color: var(--gx-text-muted);
}

/* 通用面板 */
.panel {
  background: linear-gradient(180deg, var(--gx-panel-hi) 0%, var(--gx-panel) 5%, var(--gx-panel) 100%);
  border: 1px solid var(--gx-panel-edge);
  border-radius: 10px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 2px 10px rgba(0, 0, 0, 0.35);
  padding: 16px 18px;
}
.panel-label {
  font-family: var(--gx-font-mono);
  font-size: 10px;
  color: var(--gx-text-muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 14px;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* 警告条 */
.alert {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
  margin-bottom: 14px;
  border: 1px solid;
}
.alert-led {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.alert--err {
  background: rgba(255, 92, 92, 0.08);
  border-color: rgba(255, 92, 92, 0.25);
  color: var(--gx-danger);
}
.alert--err .alert-led {
  background: var(--gx-led-red);
  box-shadow: 0 0 6px var(--gx-led-red);
}
.alert--info {
  background: rgba(61, 220, 132, 0.06);
  border-color: rgba(61, 220, 132, 0.2);
  color: var(--gx-success);
}
.alert--info .alert-led {
  background: var(--gx-led-green);
  box-shadow: 0 0 6px var(--gx-led-green);
}

/* 投料口：上传区 */
.feed-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-bottom: 18px;
}
.feed-grid {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  flex: 1;
  min-width: 220px;
}
.field-label {
  font-size: 10px;
  color: var(--gx-text-muted);
  font-family: var(--gx-font-mono);
  text-transform: uppercase;
  letter-spacing: 1px;
}
.gx-select,
.gx-input {
  padding: 9px 12px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 7px;
  font-size: 13px;
  color: var(--gx-text);
  background: var(--gx-panel-lo);
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.5);
  font-family: var(--gx-font-body);
  width: 100%;
}
.gx-select:focus,
.gx-input:focus {
  outline: none;
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.5), 0 0 0 2px var(--gx-accent);
}
.gx-select option {
  background: var(--gx-panel-lo);
  color: var(--gx-text);
}

/* 授权勾选 */
.export-check {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  font-size: 12.5px;
  color: var(--gx-text-muted);
  cursor: pointer;
  padding: 10px 12px;
  background: var(--gx-panel-lo);
  border: 1px solid var(--gx-panel-edge);
  border-radius: 7px;
  box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.4);
}
.gx-check {
  margin-top: 2px;
  min-width: 16px;
  min-height: 16px;
  accent-color: var(--gx-accent);
  cursor: pointer;
}
.check-label {
  line-height: 1.5;
}

/* 投料槽：文件选择 + 上传按钮 */
.feed-slot {
  display: flex;
  gap: 12px;
  align-items: stretch;
  flex-wrap: wrap;
}
.gx-file {
  flex: 1;
  min-width: 240px;
  padding: 9px 12px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 7px;
  font-size: 12.5px;
  color: var(--gx-text);
  background: var(--gx-panel-lo);
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.5);
}
.gx-file::file-selector-button {
  padding: 6px 12px;
  margin-right: 12px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 6px;
  background: linear-gradient(180deg, var(--gx-panel-hi), var(--gx-panel));
  color: var(--gx-text);
  font-size: 12px;
  font-family: var(--gx-font-mono);
  letter-spacing: 0.5px;
  cursor: pointer;
  transition: all 0.12s ease;
}
.gx-file::file-selector-button:active {
  transform: translateY(1px);
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.4);
}
.gx-file:focus {
  outline: none;
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.5), 0 0 0 2px var(--gx-accent);
}

/* 金属按钮 */
.gx-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 10px 16px;
  min-height: 44px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 7px;
  font-size: 13.5px;
  cursor: pointer;
  transition: all 0.12s ease;
  background: linear-gradient(180deg, var(--gx-panel-hi), var(--gx-panel));
  color: var(--gx-text);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 1px 3px rgba(0, 0, 0, 0.3);
  font-family: var(--gx-font-body);
}
.gx-btn:active {
  transform: translateY(1px);
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.4);
}
.gx-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.gx-btn--primary {
  background: linear-gradient(180deg, var(--gx-accent), var(--gx-accent-press));
  color: #2a1a00;
  font-weight: 600;
  border-color: var(--gx-accent-press);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.3), 0 0 10px rgba(255, 181, 71, 0.2), 0 1px 2px rgba(0, 0, 0, 0.4);
}
.gx-btn--ghost {
  background: linear-gradient(180deg, var(--gx-panel-hi), var(--gx-panel));
}

/* 资产清单：文件柜抽屉 */
.vault-panel {
  margin-top: 4px;
}
.drawers {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.drawer {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 14px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 8px;
  background: var(--gx-panel-lo);
  box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.4);
  transition: background 0.12s ease, border-color 0.12s ease;
}
.drawer:hover {
  background: #30343a;
  border-color: #3a3f47;
}
.drawer-led {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
}
.drawer-led.led-green {
  background: var(--gx-led-green);
  box-shadow: 0 0 6px var(--gx-led-green);
}
.drawer-led.led-amber {
  background: var(--gx-led-amber);
  box-shadow: 0 0 6px var(--gx-led-amber);
  animation: led-pulse 1.6s infinite;
}
.drawer-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.drawer-top {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.drawer-name {
  font-family: var(--gx-font-display);
  font-size: 14.5px;
  font-weight: 600;
  color: var(--gx-text);
  word-break: break-all;
}
.kind-tag {
  font-family: var(--gx-font-mono);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1px;
  padding: 2px 8px;
  border-radius: 10px;
  border: 1px solid var(--gx-panel-edge);
}
.kind-tag.kind--video {
  background: rgba(255, 181, 71, 0.12);
  color: var(--gx-led-amber);
  border-color: rgba(255, 181, 71, 0.3);
}
.kind-tag.kind--manual {
  background: rgba(90, 96, 104, 0.25);
  color: var(--gx-text-muted);
}
.kind-tag.kind--image {
  background: rgba(61, 220, 132, 0.1);
  color: var(--gx-led-green);
  border-color: rgba(61, 220, 132, 0.25);
}
.drawer-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}
.meta-sep {
  color: var(--gx-led-grey);
}
.ai-flag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--gx-font-mono);
  font-size: 11px;
}
.ai-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.ai-on {
  color: var(--gx-led-green);
}
.ai-on .ai-dot {
  background: var(--gx-led-green);
  box-shadow: 0 0 4px var(--gx-led-green);
}
.ai-off {
  color: var(--gx-text-muted);
}
.ai-off .ai-dot {
  background: var(--gx-led-grey);
}
.process-state {
  color: var(--gx-led-amber);
  font: 11px var(--gx-font-mono);
}
.process-state--done { color: var(--gx-led-green); }
.process-state--failed { color: var(--gx-danger); }
.process-state--warning { color: var(--gx-warning); }
.spinning { animation: spin 1s linear infinite; }
.drawer-btn {
  flex-shrink: 0;
  padding: 8px 14px;
  min-height: 44px;
  font-size: 12.5px;
}
.disabled-action { opacity:.5;cursor:not-allowed; }

.empty {
  padding: 24px 0;
  text-align: center;
}

@keyframes led-pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}
@keyframes spin { to { transform: rotate(360deg); } }

/* 响应式 */
@media (max-width: 768px) {
  .feed-grid {
    flex-direction: column;
  }
  .field {
    min-width: 0;
  }
  .feed-slot {
    flex-direction: column;
  }
  .gx-file {
    min-width: 0;
  }
  .drawer {
    flex-wrap: wrap;
  }
  .drawer-btn {
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .drawer-led.led-amber {
    animation: none;
  }
  .spinning { animation: none; }
  .gx-btn,
  .drawer,
  .gx-file::file-selector-button {
    transition: none;
  }
}
</style>
