// 认证与用户 Store。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api } from "@/api/client";
import type { User } from "@/api/types";

export const useAuthStore = defineStore("auth", () => {
  const user = ref<User | null>(null);
  const loading = ref(false);
  const isAuthenticated = computed(() => user.value !== null);
  const isWriter = computed(
    () =>
      user.value?.role === "admin" ||
      user.value?.role === "instructor",
  );

  async function login(username: string, password: string): Promise<User> {
    loading.value = true;
    try {
      const me = await api.post<User>("/auth/login", { username, password });
      user.value = me;
      return me;
    } finally {
      loading.value = false;
    }
  }

  async function fetchMe(): Promise<boolean> {
    try {
      user.value = await api.get<User>("/auth/me");
      return true;
    } catch {
      user.value = null;
      return false;
    }
  }

  async function logout(): Promise<void> {
    try {
      await api.post("/auth/logout");
    } finally {
      user.value = null;
    }
  }

  return { user, loading, isAuthenticated, isWriter, login, fetchMe, logout };
});
