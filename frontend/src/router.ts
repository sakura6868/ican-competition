import { createRouter, createWebHistory } from "vue-router";

import { useAuthStore } from "@/stores/auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/login",
      name: "login",
      component: () => import("@/views/LoginView.vue"),
      meta: { public: true },
    },
    {
      path: "/",
      component: () => import("@/layouts/MainLayout.vue"),
      children: [
        { path: "", name: "workbench", component: () => import("@/views/WorkbenchView.vue") },
        { path: "knowledge", name: "knowledge", component: () => import("@/views/KnowledgeView.vue") },
        { path: "sops", name: "sop-studio", component: () => import("@/views/SopStudioView.vue") },
        { path: "teaching-analytics", name: "teaching-analytics", component: () => import("@/views/TeacherAnalyticsView.vue") },
        { path: "assessment", name: "assessment", component: () => import("@/views/AssessmentView.vue") },
        { path: "practice", name: "practice", component: () => import("@/views/PracticeView.vue") },
        { path: "practice/:sessionId", name: "practice-session", component: () => import("@/views/PracticeView.vue") },
        { path: "data-models", name: "data-models", component: () => import("@/views/DataModelsView.vue") },
        { path: "admin-users", name: "admin-users", component: () => import("@/views/AdminUsersView.vue") },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (auth.user === null) {
    // 未初始化时尝试从会话恢复
    await auth.fetchMe();
  }
  if (to.meta.public) {
    if (auth.isAuthenticated && to.name === "login") {
      return { name: "workbench" };
    }
    return true;
  }
  if (!auth.isAuthenticated) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  return true;
});

export default router;
