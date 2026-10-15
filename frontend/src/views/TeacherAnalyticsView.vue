<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { AlertTriangle, BarChart3, CheckCircle2, Clock3, RefreshCw, Users } from "lucide-vue-next";

import { api } from "@/api/client";
import type { LearningReport } from "@/api/practice";

interface AnalyticsSession {
  id: string;
  learner_name: string;
  course_title: string;
  status: string;
  created_at: string;
  average_latency_ms: number | null;
  report: LearningReport;
}

interface AnalyticsData {
  summary: { learner_count: number; session_count: number; finished_count: number; completion_rate: number; correction_count: number; provider_call_count: number };
  weak_steps: { sop_id: string; course_title: string; step_index: number; name: string; learner_count: number; corrections: number; unknowns: number }[];
  sessions: AnalyticsSession[];
  method_note: string;
}

const data = ref<AnalyticsData | null>(null);
const loading = ref(false);
const error = ref("");
const recent = computed(() => data.value?.sessions.slice(0, 20) ?? []);

async function load() {
  loading.value = true;
  error.value = "";
  try { data.value = await api.get<AnalyticsData>("/practice-sessions/analytics"); }
  catch (reason) { error.value = reason instanceof Error ? reason.message : "学情数据加载失败"; }
  finally { loading.value = false; }
}

onMounted(load);
</script>

<template>
  <section class="analytics-page">
    <header class="page-head"><div><span>TEACHING INSIGHTS</span><h1>班级学情看板</h1><p>从逐步练习记录定位共性难点，并进入单次报告查看证据。</p></div><button class="refresh" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新数据</button></header>
    <p v-if="error" class="alert">{{ error }}</p>
    <div class="metric-grid">
      <article><Users :size="20" /><span>参与学员</span><strong>{{ data?.summary.learner_count ?? '—' }}</strong></article>
      <article><BarChart3 :size="20" /><span>练习次数</span><strong>{{ data?.summary.session_count ?? '—' }}</strong></article>
      <article><CheckCircle2 :size="20" /><span>完整完成率</span><strong>{{ data ? `${data.summary.completion_rate}%` : '—' }}</strong></article>
      <article><RefreshCw :size="20" /><span>视觉核验调用</span><strong>{{ data?.summary.provider_call_count ?? '—' }}</strong></article>
    </div>
    <div class="analytics-grid">
      <section class="panel weak-panel"><div class="panel-title"><AlertTriangle :size="18" /><div><h2>需要加强的步骤</h2><p>出现纠错、连续看不清或明显超时的步骤</p></div></div><div v-if="data?.weak_steps.length" class="weak-list"><article v-for="item in data.weak_steps.slice(0, 8)" :key="`${item.sop_id}-${item.step_index}`"><span>{{ String(item.step_index).padStart(2,'0') }}</span><div><strong>{{ item.name }}</strong><small>{{ item.course_title }}</small></div><em>{{ item.learner_count }} 人 · {{ item.corrections }} 次纠错 · {{ item.unknowns }} 次补拍</em></article></div><p v-else class="empty">暂无需要加强的步骤。</p></section>
      <section class="panel method"><Clock3 :size="20" /><h2>统计口径</h2><p>{{ data?.method_note || '正在读取统计口径…' }}</p><small>核验时延只统计已保存 Provider 收据的调用；未配置价格时不估算费用。</small></section>
    </div>
    <section class="panel sessions"><div class="panel-title"><BarChart3 :size="18" /><div><h2>最近练习</h2><p>点击进入学员的逐步学习报告</p></div></div><div class="table-head"><span>学员 / 课程</span><span>状态</span><span>完成</span><span>纠错</span><span>平均核验</span><span>时间</span></div><RouterLink v-for="item in recent" :key="item.id" :to="`/practice/${item.id}`" class="table-row"><span><strong>{{ item.learner_name }}</strong><small>{{ item.course_title }}</small></span><b :class="item.status">{{ item.status === 'finished' ? '已完成' : item.status === 'active' ? '练习中' : item.status === 'paused' ? '已暂停' : '已结束' }}</b><span>{{ item.report.completion_rate }}%</span><span>{{ item.report.correction_count }} 次</span><span>{{ item.average_latency_ms ? `${(item.average_latency_ms/1000).toFixed(1)}s` : '—' }}</span><span>{{ new Date(item.created_at).toLocaleString('zh-CN') }}</span></RouterLink><p v-if="!recent.length" class="empty">还没有学员练习记录。</p></section>
  </section>
</template>

<style scoped>
.analytics-page{max-width:1380px}.page-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px}.page-head span{font-size:9px;letter-spacing:1.7px;color:var(--gx-text-muted)}.page-head h1{font-size:26px;margin:8px 0 5px}.page-head p,.panel p{font-size:11px;color:var(--gx-text-muted);margin:0}.refresh{display:flex;align-items:center;gap:7px;min-height:40px;padding:8px 14px;border:1px solid var(--gx-border);border-radius:9px;background:white}.alert{padding:12px;background:#fff2f3;color:var(--gx-danger);border-radius:9px}.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}.metric-grid article{display:grid;grid-template-columns:42px 1fr;align-items:center;gap:2px 12px;padding:18px;background:white;border:1px solid var(--gx-border);border-radius:13px}.metric-grid svg{grid-row:1/3;width:42px;height:42px;padding:11px;border-radius:11px;background:var(--gx-accent-soft);color:var(--gx-accent)}.metric-grid span{font-size:10px;color:var(--gx-text-muted)}.metric-grid strong{font-size:24px}.analytics-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px;margin-bottom:20px}.panel{background:white;border:1px solid var(--gx-border);border-radius:14px;padding:20px}.panel-title{display:flex;gap:10px;align-items:flex-start;margin-bottom:16px}.panel-title svg{color:var(--gx-accent)}.panel-title h2,.method h2{font-size:14px;margin:0 0 5px}.weak-list article{display:grid;grid-template-columns:34px minmax(0,1fr) auto;gap:11px;align-items:center;padding:12px 0;border-top:1px solid var(--gx-border)}.weak-list article>span{width:30px;height:30px;display:grid;place-items:center;border-radius:8px;background:#fff0e2;color:#af7845;font-size:10px}.weak-list strong,.weak-list small{display:block}.weak-list strong{font-size:11px}.weak-list small,.weak-list em{font-size:9px;color:var(--gx-text-muted);font-style:normal}.method>svg{color:var(--gx-accent);margin-bottom:16px}.method p{line-height:1.9}.method small{display:block;margin-top:18px;font-size:9px;line-height:1.8;color:#949aac}.table-head,.table-row{display:grid;grid-template-columns:minmax(190px,1.6fr) .6fr .5fr .5fr .7fr 1fr;gap:12px;align-items:center}.table-head{padding:8px 10px;color:var(--gx-text-muted);font-size:9px}.table-row{padding:13px 10px;border-top:1px solid var(--gx-border);color:var(--gx-text);font-size:10px}.table-row strong,.table-row small{display:block}.table-row small{margin-top:3px;color:var(--gx-text-muted)}.table-row b{font-size:9px;color:#7c8496}.table-row b.finished{color:var(--gx-success)}.table-row:hover{background:#faf9ff}.empty{padding:22px;text-align:center;color:var(--gx-text-muted)}
@media(max-width:1050px){.metric-grid{grid-template-columns:repeat(2,1fr)}.analytics-grid{grid-template-columns:1fr}.table-head,.table-row{grid-template-columns:minmax(170px,1.5fr) .7fr .5fr .5fr .7fr}.table-head span:last-child,.table-row>span:last-child{display:none}}
</style>
