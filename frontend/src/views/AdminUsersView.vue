<script setup lang="ts">
import { onMounted, ref } from "vue";
import { UserPlus } from "lucide-vue-next";

import { api } from "@/api/client";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();

interface AdminUser {
  id: string;
  username: string;
  display_name: string;
  role: string;
  is_active: boolean;
}

const users = ref<AdminUser[]>([]);
const error = ref("");
const info = ref("");

const username = ref("");
const password = ref("");
const displayName = ref("");
const role = ref<"admin" | "instructor" | "learner" | "observer">("learner");

async function load() {
  users.value = await api.get<AdminUser[]>("/admin/users");
}

async function onCreate() {
  error.value = "";
  info.value = "";
  try {
    await api.post<AdminUser>("/admin/users", {
      username: username.value,
      password: password.value,
      role: role.value,
      display_name: displayName.value,
    });
    info.value = `已创建账号 ${username.value}`;
    username.value = "";
    password.value = "";
    displayName.value = "";
    await load();
  } catch (e) {
    error.value = e instanceof Error ? e.message : "创建失败";
  }
}

async function onToggle(u: AdminUser) {
  try {
    await api.patch<AdminUser>(`/admin/users/${u.id}`, { is_active: !u.is_active });
    await load();
  } catch (e) {
    error.value = e instanceof Error ? e.message : "更新失败";
  }
}

onMounted(load);
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div class="head-led" aria-hidden="true"></div>
      <div>
        <h1>账号与设置</h1>
        <p class="muted">管理员创建账号并分配角色：admin / instructor / learner / observer。</p>
      </div>
    </header>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="info" class="info">{{ info }}</p>

    <div class="card">
      <h2><UserPlus :size="16" /> 新建账号</h2>
      <form class="form" @submit.prevent="onCreate">
        <label>
          用户名
          <input v-model="username" type="text" required />
        </label>
        <label>
          密码
          <input v-model="password" type="password" minlength="8" required />
        </label>
        <label>
          显示名
          <input v-model="displayName" type="text" />
        </label>
        <label>
          角色
          <select v-model="role">
            <option value="admin">admin</option>
            <option value="instructor">instructor</option>
            <option value="learner">learner</option>
            <option value="observer">observer</option>
          </select>
        </label>
        <button class="primary" type="submit">创建</button>
      </form>
    </div>

    <div class="card">
      <h2>账号列表（{{ users.length }}）</h2>
      <table class="table">
        <thead>
          <tr>
            <th>用户名</th>
            <th>显示名</th>
            <th>角色</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td>{{ u.username }}</td>
            <td>{{ u.display_name || "—" }}</td>
            <td>{{ u.role }}</td>
            <td>{{ u.is_active ? "启用" : "停用" }}</td>
            <td>
              <button
                class="mini"
                :disabled="u.id === auth.user?.id"
                @click="onToggle(u)"
              >
                {{ u.is_active ? "停用" : "启用" }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.page { max-width: 1100px; }
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
  margin: 0 0 4px;
  font-family: var(--gx-font-display);
  font-size: 26px;
  letter-spacing: 0.5px;
}
.muted {
  color: var(--gx-text-muted);
  font-size: 13px;
}
.error {
  color: var(--gx-danger);
}
.info {
  color: var(--gx-success);
}
.card {
  background: linear-gradient(180deg, var(--gx-panel-hi) 0%, var(--gx-panel) 5%, var(--gx-panel) 100%); box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 2px 10px rgba(0,0,0,0.35);
  border: 1px solid var(--gx-panel-edge);
  border-radius: 10px;
  padding: 20px;
  margin-top: 16px;
}
.card h2 {
  font-size: 16px;
  margin: 0 0 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  align-items: end;
}
.form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.form input,
.form select {
  padding: 8px 10px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 8px;
  font-size: 14px;
}
.primary {
  padding: 10px 14px;
  min-height: 44px; /* 触控目标（整改 R8.7） */
  border: none;
  border-radius: 8px;
  background: var(--gx-accent);
  color: #2a1a00; font-weight: 600;
  font-size: 14px;
  cursor: pointer;
}
.table {
  width: 100%;
  border-collapse: collapse;
}
.table th,
.table td {
  text-align: left;
  padding: 8px 10px;
  font-size: 13px;
  border-bottom: 1px solid var(--gx-border);
}
.mini {
  padding: 5px 10px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 6px;
  background: linear-gradient(180deg, var(--gx-panel-hi) 0%, var(--gx-panel) 5%, var(--gx-panel) 100%); box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 2px 10px rgba(0,0,0,0.35);
  cursor: pointer;
  font-size: 13px;
}
.mini:disabled {
  opacity: 0.4;
}
</style>
