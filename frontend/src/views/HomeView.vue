<script setup lang="ts">
import { onMounted, ref } from "vue";

const health = ref<string>("检查中…");
const apiPrefix = ref<string>("");

onMounted(async () => {
  apiPrefix.value = `${window.location.origin}/api/v1/health`;
  try {
    const resp = await fetch("/api/v1/health");
    const body = await resp.json();
    health.value = resp.ok ? `ok — ${body.app} ${body.version}` : `失败 ${resp.status}`;
  } catch (e) {
    health.value = `无法连接后端：${e instanceof Error ? e.message : String(e)}`;
  }
});
</script>

<template>
  <main class="home">
    <h1>工序智传</h1>
    <p class="sub">将标准教学视频与手册编译为结构化 SOP，并对新人操作进行步骤级、证据化考评。</p>

    <section class="card">
      <h2>后端健康检查</h2>
      <p>
        状态：<code>{{ health }}</code>
      </p>
      <p class="muted">
        仅当后端已启动时返回 ok（<code>{{ apiPrefix }}</code>）。
      </p>
    </section>

    <section class="card">
      <h2>开发入口</h2>
      <ul>
        <li><RouterLink to="/login">登录</RouterLink>（阶段 2 实现账号底座）</li>
        <li>工作台、知识导入、SOP 编译室等页面在阶段 6 / 11 实现。</li>
      </ul>
    </section>
  </main>
</template>

<style scoped>
.home {
  max-width: 720px;
  margin: 0 auto;
  padding: 48px 24px;
}
h1 {
  font-size: 28px;
  margin: 0 0 8px;
}
.sub {
  color: var(--gx-text-muted);
  margin: 0 0 32px;
}
.card {
  background: var(--gx-surface);
  border: 1px solid var(--gx-border);
  border-radius: 10px;
  padding: 20px;
  margin-bottom: 16px;
}
.card h2 {
  font-size: 16px;
  margin: 0 0 8px;
}
.muted {
  color: var(--gx-text-muted);
  font-size: 13px;
}
</style>
