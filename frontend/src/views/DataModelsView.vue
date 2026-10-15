<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Activity, CloudCog, Cpu, Database, RefreshCw } from "lucide-vue-next";

import { api } from "@/api/client";
import { datasetCatalog } from "@/services/displayNames";

interface ModelInfo {
  id: string;
  name: string;
  kind: string;
  status: string;
  format: string;
  version: string;
  sha256: string;
  latency_ms: number;
  notes: string;
}

interface Capability {
  key: string;
  label: string;
  status: string;
  note: string;
}

interface ProviderStatus {
  provider: string;
  configured: boolean;
  runtime_backend: string;
  mode: string;
  model_id: string;
  vision_ok: boolean | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  note: string;
}

const models = ref<ModelInfo[]>([]);
const capabilities = ref<Capability[]>([]);
const provider = ref<ProviderStatus | null>(null);
const probing = ref(false);
const error = ref("");
const dataStatus = ref("");

async function load() {
  try {
    const [m, caps, providerStatus] = await Promise.all([
      api.get<ModelInfo[]>("/models/info"),
      api.get<Capability[]>("/system/capabilities"),
      api.get<ProviderStatus>("/system/provider-status"),
    ]);
    models.value = m;
    capabilities.value = caps;
    provider.value = providerStatus;
  } catch (e) {
    error.value = e instanceof Error ? e.message : "加载失败";
  }
  dataStatus.value = "公开数据仅在本机授权目录中使用，不随仓库分发，也不会自动发送给 Qwen。只有上传者明确勾选授权后，系统才会发送压缩关键帧；数据存在不代表已经取得准确率。";
}

async function probeProvider() {
  probing.value = true;
  error.value = "";
  try {
    provider.value = await api.post<ProviderStatus>("/system/provider-probe");
  } catch (e) {
    error.value = e instanceof Error ? e.message : "Qwen 探测失败";
  } finally {
    probing.value = false;
  }
}

function statusLabel(s: string): string {
  return { implemented: "已实现", integrated: "已集成", validated: "已验证", contest_ready: "可考评" }[s] ?? s;
}

function capLedClass(s: string): string {
  if (s === "integrated" || s === "validated") return "green";
  if (s === "contest_ready") return "green-strong";
  if (s === "implemented") return "amber";
  if (s === "missing") return "red";
  return "grey";
}

function modelLedClass(s: string): string {
  if (s === "available") return "green";
  if (s === "experimental") return "amber";
  if (s === "missing") return "red";
  return "grey";
}

onMounted(load);
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div class="head-led" aria-hidden="true"></div>
      <div>
        <h1>数据与模型</h1>
        <p class="muted">
          Qwen 多模态服务、评分能力与评测数据边界。
        </p>
      </div>
    </header>

    <p v-if="error" class="alert alert--err"><span class="alert-led"></span>{{ error }}</p>

    <div class="panel provider-panel">
      <div class="provider-copy">
        <div class="panel-label"><CloudCog :size="12" /> QWEN PROVIDER · 主模型服务</div>
        <div class="provider-title">
          <span class="provider-led" :class="provider?.configured ? 'led--green' : 'led--red'"></span>
          <strong>{{ provider?.model_id || "Qwen 未配置" }}</strong>
          <span class="provider-mode">{{ provider?.runtime_backend || "none" }}</span>
        </div>
        <p class="data-status">{{ provider?.note || "正在读取 Qwen 状态" }}</p>
        <div v-if="provider?.vision_ok !== null" class="provider-metrics">
          <span><b>{{ provider?.vision_ok ? "通过" : "失败" }}</b>视觉探测</span>
          <span><b>{{ provider?.input_tokens || 0 }}</b>输入 token</span>
          <span><b>{{ provider?.output_tokens || 0 }}</b>输出 token</span>
        </div>
      </div>
      <button type="button" class="probe-button" :disabled="probing || !provider?.configured" @click="probeProvider">
        <RefreshCw :size="17" :class="{ spinning: probing }" />
        {{ probing ? "正在探测" : "探测视觉能力" }}
      </button>
    </div>

    <!-- 公开数据 -->
    <div class="panel">
      <div class="panel-label"><Database :size="12" /> PUBLIC DATA · 公开数据</div>
      <p class="data-status">{{ dataStatus }}</p>
      <div class="dataset-list" aria-label="本机公开数据目录">
        <article v-for="dataset in datasetCatalog" :key="dataset.id" class="dataset-row">
          <div>
            <strong>{{ dataset.name }}</strong>
            <small>来源：{{ dataset.sourceName }}</small>
          </div>
          <p>{{ dataset.description }}</p>
          <p class="dataset-use">{{ dataset.recommendedUse }}</p>
          <code>{{ dataset.localPath }}</code>
          <span class="dataset-status" :class="dataset.mediaAvailable ? 'dataset-status--ready' : 'dataset-status--meta'">
            {{ dataset.mediaAvailable ? "本机视频可用" : "仅标注资料" }}
          </span>
        </article>
      </div>
    </div>

    <!-- 系统能力 LED 矩阵 -->
    <div class="panel">
      <div class="panel-label"><Cpu :size="12" /> CAPABILITIES · 系统能力状态</div>
      <div class="cap-matrix">
        <div v-for="c in capabilities" :key="c.key" class="cap-row">
          <span class="cap-led" :class="`led--${capLedClass(c.status)}`" aria-hidden="true"></span>
          <div class="cap-body">
            <div class="cap-head">
              <span class="cap-label">{{ c.label }}</span>
              <span class="cap-status" :class="`cap-status--${capLedClass(c.status)}`">
                {{ statusLabel(c.status) }}
              </span>
            </div>
            <div class="cap-note muted">{{ c.note }}</div>
          </div>
        </div>
      </div>
      <p class="muted small legend">
        级别说明：已实现=代码可用；已集成=真实输入端到端跑通；已验证=独立测试集有可复现指标；可考评=可正式自动考评。
      </p>
    </div>

    <!-- 兼容性组件登记表 -->
    <div class="panel">
      <div class="panel-label">
        <Activity :size="12" /> OPTIONAL COMPONENTS · 可选兼容组件（{{ models.length }}）
      </div>
      <table v-if="models.length" class="table">
        <thead>
          <tr>
            <th>模型</th>
            <th>类型</th>
            <th>状态</th>
            <th>格式</th>
            <th>版本</th>
            <th>SHA-256</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in models" :key="m.id">
            <td class="model-name">{{ m.name }}</td>
            <td>{{ m.kind }}</td>
            <td>
              <span class="model-led-cell">
                <span class="model-led" :class="`led--${modelLedClass(m.status)}`" aria-hidden="true"></span>
                <span class="model-status" :class="`model-status--${modelLedClass(m.status)}`">{{ m.status }}</span>
              </span>
            </td>
            <td>{{ m.format }}</td>
            <td class="mono">{{ m.version || "—" }}</td>
            <td class="mono sha">{{ m.sha256 ? m.sha256.slice(0, 12) + "…" : "—" }}</td>
            <td class="muted">{{ m.notes }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted empty">没有可选本地兼容组件；主体功能继续使用 Qwen。</p>
    </div>
  </section>
</template>

<style scoped>
.page {
  max-width: 1200px;
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

/* 通用面板（拟物凸起） */
.panel {
  background: linear-gradient(180deg, var(--gx-panel-hi) 0%, var(--gx-panel) 5%);
  border: 1px solid var(--gx-panel-edge);
  border-radius: 10px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 2px 10px rgba(0, 0, 0, 0.35);
  padding: 16px 18px;
  margin-top: 16px;
}
.panel-label {
  font-family: var(--gx-font-mono);
  font-size: 10px;
  color: var(--gx-text-muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* 公开数据 */
.data-status {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--gx-text);
}

.provider-panel {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
.provider-copy {
  min-width: 0;
  flex: 1;
}
.provider-title {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 8px;
}
.provider-title strong {
  font-family: var(--gx-font-display);
  font-size: 19px;
}
.provider-led {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex: 0 0 auto;
}
.provider-mode {
  padding: 3px 7px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 5px;
  color: var(--gx-text-muted);
  font-family: var(--gx-font-mono);
  font-size: 10px;
  text-transform: uppercase;
}
.provider-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 12px;
  color: var(--gx-text-muted);
  font-size: 12px;
}
.provider-metrics b {
  margin-right: 5px;
  color: var(--gx-led-green);
  font-family: var(--gx-font-mono);
}
.probe-button {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 1px solid rgba(61, 220, 132, 0.35);
  border-radius: 7px;
  padding: 0 15px;
  background: rgba(61, 220, 132, 0.1);
  color: var(--gx-led-green);
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}
.probe-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.spinning {
  animation: spin 0.8s linear infinite;
}

/* 能力 LED 矩阵 */
.cap-matrix {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cap-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 12px;
  background: var(--gx-panel-lo);
  border: 1px solid var(--gx-panel-edge);
  border-radius: 8px;
  box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.3);
  transition: background 0.12s ease;
}
.cap-row:hover {
  background: #30343a;
}
.cap-led {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
  margin-top: 4px;
}
.cap-body {
  flex: 1;
  min-width: 0;
}
.cap-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.cap-label {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--gx-text);
}
.cap-status {
  font-family: var(--gx-font-mono);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1px;
  padding: 2px 8px;
  border-radius: 10px;
}
.cap-status--green,
.cap-status--green-strong {
  color: var(--gx-led-green);
  background: rgba(61, 220, 132, 0.12);
}
.cap-status--amber {
  color: var(--gx-led-amber);
  background: rgba(255, 181, 71, 0.1);
}
.cap-status--red {
  color: var(--gx-led-red);
  background: rgba(255, 92, 92, 0.12);
}
.cap-status--grey {
  color: var(--gx-text-muted);
  background: rgba(90, 96, 104, 0.2);
}
.cap-note {
  margin-top: 3px;
  font-size: 12px;
  line-height: 1.5;
}
.legend {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.05);
  line-height: 1.6;
}

/* LED 通用（绿/琥珀脉冲/红/灰） */
.led--green {
  background: var(--gx-led-green);
  box-shadow: 0 0 7px var(--gx-led-green);
}
.led--green-strong {
  background: var(--gx-led-green);
  box-shadow: 0 0 12px var(--gx-led-green), 0 0 4px var(--gx-led-green);
}
.led--amber {
  background: var(--gx-led-amber);
  box-shadow: 0 0 7px var(--gx-led-amber);
  animation: led-pulse 1.8s infinite;
}
.led--red {
  background: var(--gx-led-red);
  box-shadow: 0 0 7px var(--gx-led-red);
}
.led--grey {
  background: var(--gx-led-grey);
}

/* 模型登记表 */
.table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 4px;
}
.table th {
  text-align: left;
  padding: 8px 10px;
  font-size: 10px;
  font-family: var(--gx-font-mono);
  color: var(--gx-text-muted);
  letter-spacing: 1px;
  text-transform: uppercase;
  border-bottom: 1px solid var(--gx-panel-edge);
}
.table td {
  padding: 9px 10px;
  font-size: 12.5px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  color: var(--gx-text);
}
.table tbody tr {
  background: var(--gx-panel-lo);
  box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.25);
}
.table tbody tr:hover {
  background: #30343a;
}
.model-name {
  font-weight: 600;
  color: var(--gx-text);
}
.model-led-cell {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}
.model-led {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.model-status {
  font-family: var(--gx-font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.model-status--green {
  color: var(--gx-led-green);
}
.model-status--amber {
  color: var(--gx-led-amber);
}
.model-status--red {
  color: var(--gx-led-red);
}
.sha {
  color: var(--gx-text-muted);
}
.empty {
  padding: 16px 0;
}
.dataset-list { display: grid; gap: 9px; margin-top: 14px; }
.dataset-row { display: grid; grid-template-columns: minmax(180px, .85fr) minmax(220px, 1.2fr) minmax(220px, 1.1fr) minmax(180px, .8fr) auto; gap: 12px; align-items: center; padding: 11px 12px; border: 1px solid var(--gx-panel-edge); border-radius: 7px; background: var(--gx-panel-lo); }
.dataset-row > div { display: grid; gap: 3px; }
.dataset-row strong { color: var(--gx-text); font-size: 13px; }
.dataset-row small, .dataset-row p { margin: 0; color: var(--gx-text-muted); font-size: 12px; line-height: 1.45; }
.dataset-row .dataset-use { color: var(--gx-text); }
.dataset-row code { color: var(--gx-accent); font: 10px var(--gx-font-mono); overflow-wrap: anywhere; }
.dataset-status { justify-self: end; white-space: nowrap; padding: 4px 7px; border: 1px solid; border-radius: 5px; font: 10px var(--gx-font-mono); }
.dataset-status--ready { color: var(--gx-led-green); border-color: rgba(61,220,132,.42); }
.dataset-status--meta { color: var(--gx-led-amber); border-color: rgba(255,181,71,.42); }

@keyframes led-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 768px) {
  .page-head h1 {
    font-size: 22px;
  }
  .panel {
    padding: 14px 14px;
  }
  .table {
    display: block;
    overflow-x: auto;
    white-space: nowrap;
  }
  .cap-row {
    padding: 9px 10px;
  }
  .provider-panel {
    align-items: stretch;
    flex-direction: column;
  }
  .probe-button {
    width: 100%;
  }
  .dataset-row { grid-template-columns: 1fr; }
  .dataset-status { justify-self: start; }
}

@media (prefers-reduced-motion: reduce) {
  .led--amber,
  .cap-row,
  .table tbody tr {
    animation: none;
    transition: none;
  }
  .spinning {
    animation: none;
  }
}
</style>
