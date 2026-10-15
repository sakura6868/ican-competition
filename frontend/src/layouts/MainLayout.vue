<!-- Adapted from primefaces/sakai-vue AppLayout.vue (MIT).
     Copyright (c) 2018-2022 PrimeTek. See LICENSES/sakai-MIT.txt. -->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";
import { ArrowRight, BarChart3, BookOpen, Camera, ChevronDown, ChevronRight, ClipboardCheck, Database, GraduationCap, LayoutDashboard, LogOut, Menu, Sparkles, Users, Video, Workflow, X } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useEducationLayout } from "./useEducationLayout";
const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const { layoutState, toggleMenu, hideMobileMenu, isDesktop } = useEducationLayout();
const desktop = ref(isDesktop());
const menuButton = ref<HTMLButtonElement | null>(null);
const sidebar = ref<HTMLElement | null>(null);
const canTeach = computed(() => ["admin", "instructor"].includes(auth.user?.role || ""));
const roleNames = { admin: "管理员", instructor: "教师", learner: "学员", observer: "观察员" };
const userName = computed(() => auth.user?.display_name || auth.user?.username || "同学");
const navGroups = computed(() => [
  { label: "学习空间", items: [
    { to: "/", label: "学习总览", icon: LayoutDashboard },
    { to: "/practice", label: "课程与 AI 陪练", icon: BookOpen },
    { to: "/assessment", label: "学习评估", icon: ClipboardCheck },
  ] },
  ...(canTeach.value ? [{ label: "教师工作室", items: [
    { to: "/knowledge", label: "教学素材", icon: Video },
    { to: "/sops", label: "SOP 课程编排", icon: Workflow },
    { to: "/teaching-analytics", label: "班级学情", icon: BarChart3 },
    { to: "/data-models", label: "数据与模型", icon: Database },
  ] }] : []),
  ...(auth.user?.role === "admin" ? [{ label: "管理", items: [{ to: "/admin-users", label: "成员与权限", icon: Users }] }] : []),
]);
const pageTitle = computed(() => route.name === "practice-session" ? "实训课堂" : navGroups.value.flatMap(g => g.items).find(item => item.to === route.path)?.label || "学习空间");
const containerClass = computed(() => ({ "layout-static": true, "layout-static-inactive": layoutState.staticMenuInactive, "layout-mobile-active": layoutState.mobileMenuActive }));
async function closeMenu() { hideMobileMenu(); await nextTick(); menuButton.value?.focus(); }
function onResize() { desktop.value = isDesktop(); if (desktop.value) hideMobileMenu(); }
function onKey(event: KeyboardEvent) {
  if (!layoutState.mobileMenuActive) return;
  if (event.key === "Escape") { event.preventDefault(); closeMenu(); }
  if (event.key === "Tab") {
    const elements = Array.from(sidebar.value?.querySelectorAll<HTMLElement>('a[href], button:not([disabled])') || []);
    const first = elements[0], last = elements.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
}
watch(() => route.fullPath, hideMobileMenu);
watch(() => layoutState.mobileMenuActive, async (open) => { document.body.style.overflow = open ? "hidden" : ""; if (open) { await nextTick(); sidebar.value?.querySelector<HTMLElement>("button")?.focus(); } });
onMounted(() => { window.addEventListener("resize", onResize); window.addEventListener("keydown", onKey); });
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); window.removeEventListener("keydown", onKey); document.body.style.overflow = ""; hideMobileMenu(); });
async function onLogout() { hideMobileMenu(); await auth.logout(); router.push({ name: "login" }); }
</script>
<template>
  <div class="layout-wrapper" :class="containerClass">
    <a href="#main-content" class="skip-link">跳到主要内容</a>
    <header class="layout-topbar" :inert="layoutState.mobileMenuActive">
      <RouterLink to="/" class="brand" aria-label="工序智传，学习首页"><span class="brand-icon"><GraduationCap :size="25" /></span><span><strong>工序智传<span class="brand-dot">.</span></strong><small>智能技能教育平台</small></span></RouterLink>
      <button ref="menuButton" class="icon-button menu-toggle" :aria-expanded="desktop ? !layoutState.staticMenuInactive : layoutState.mobileMenuActive" aria-controls="education-sidebar" aria-label="切换导航菜单" @click="toggleMenu"><Menu :size="20" /></button>
      <div class="breadcrumb"><span>学习空间</span><ChevronRight :size="13" /><strong>{{ pageTitle }}</strong></div>
      <div class="topbar-right"><span class="ai-label"><Sparkles :size="14" />AI 辅助学习</span><details class="account-menu"><summary><span class="avatar">{{ userName.slice(0, 1) }}</span><span class="account-name">{{ userName }}</span><ChevronDown :size="14" /></summary><div class="account-popover"><strong>{{ userName }}</strong><small>{{ auth.user ? roleNames[auth.user.role] : '' }}</small><button @click="onLogout"><LogOut :size="15" />退出登录</button></div></details></div>
    </header>
    <aside id="education-sidebar" ref="sidebar" class="layout-sidebar" :inert="desktop ? layoutState.staticMenuInactive : !layoutState.mobileMenuActive">
      <div class="mobile-sidebar-head"><span>学习空间</span><button class="icon-button" aria-label="关闭导航" @click="closeMenu"><X :size="20" /></button></div>
      <nav aria-label="主导航"><div v-for="group in navGroups" :key="group.label" class="nav-group"><div class="nav-label">{{ group.label }}</div><RouterLink v-for="item in group.items" :key="item.to" :to="item.to" class="nav-item" :class="{ active: item.to === '/' ? route.path === '/' : route.path.startsWith(item.to) }"><component :is="item.icon" :size="19" /><span>{{ item.label }}</span><span v-if="item.to === '/practice'" class="nav-ai" aria-hidden="true">AI</span></RouterLink></div></nav>
      <div class="sidebar-bottom"><div class="study-note"><span class="study-icon"><Camera :size="20" /></span><strong>让技能，真正学会</strong><p>看示范 · 动手练 · 及时纠错</p><RouterLink to="/practice">开启一次实训<ArrowRight :size="15" /></RouterLink></div><button class="sidebar-logout" @click="onLogout"><LogOut :size="17" /><span>退出登录</span></button></div>
    </aside>
    <div class="layout-main-container" :inert="layoutState.mobileMenuActive">
      <main id="main-content" class="layout-main" tabindex="-1"><RouterView /></main>
      <footer class="layout-footer"><span>工序智传 · 让每一次练习都有反馈</span><a href="/open-source.txt" target="_blank" rel="noopener">开源与许可</a></footer>
    </div>
    <button v-if="layoutState.mobileMenuActive" class="layout-mask" aria-label="关闭导航遮罩" tabindex="-1" @click="closeMenu"></button>
  </div>
</template>
<style scoped>
.layout-wrapper{min-height:100vh}.layout-topbar{height:84px;position:fixed;z-index:50;top:0;left:0;right:0;display:flex;align-items:center;gap:24px;padding:0 30px;background:white;border-bottom:1px solid var(--gx-border)}.brand{display:flex;gap:11px;align-items:center;flex:0 0 224px;color:var(--gx-text)}.brand-icon{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:var(--gx-accent);color:white;box-shadow:0 5px 12px #6055d826}.brand strong{font-size:21px;font-weight:800;letter-spacing:.5px;line-height:1.1}.brand-dot{color:var(--gx-accent)}.brand small{display:block;font-size:10px;letter-spacing:2px;color:var(--gx-text-muted);margin-top:3px}.icon-button{width:44px;height:44px;display:grid;place-items:center;border:1px solid var(--gx-border);background:white;color:var(--gx-text-muted);border-radius:10px;flex-shrink:0}.icon-button:hover{background:var(--gx-panel-lo);color:var(--gx-accent)}.breadcrumb{display:flex;align-items:center;gap:10px;color:var(--gx-text-muted);font-size:12px}.breadcrumb strong{font-weight:500;color:var(--gx-text)}.topbar-right{margin-left:auto;display:flex;align-items:center;gap:26px}.ai-label{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--gx-accent);padding:6px 10px;background:var(--gx-accent-soft);border-radius:7px}.account-menu{position:relative}.account-menu summary{display:flex;align-items:center;gap:10px;cursor:pointer;list-style:none;font-size:12px}.account-menu summary::-webkit-details-marker{display:none}.avatar{width:34px;height:34px;display:grid;place-items:center;border-radius:50%;background:#eaf3ef;color:#258069;font-weight:700}.account-popover{position:absolute;top:48px;right:0;min-width:190px;padding:16px;display:grid;gap:7px;border-radius:12px;background:white;border:1px solid var(--gx-border);box-shadow:0 10px 30px #20263b12}.account-popover small{color:var(--gx-text-muted)}.account-popover button{display:flex;gap:8px;align-items:center;border:0;background:var(--gx-panel-lo);color:var(--gx-text);padding:9px;border-radius:6px;margin-top:8px}.layout-sidebar{position:fixed;z-index:40;top:108px;left:22px;bottom:22px;width:236px;display:flex;flex-direction:column;gap:12px;padding:15px 12px;background:white;border:1px solid var(--gx-border);border-radius:16px;transition:transform .2s;overflow-y:auto}.nav-group{margin-bottom:22px}.nav-label{font-size:10px;font-weight:500;letter-spacing:1px;color:#9098aa;padding:6px 15px 10px}.nav-item{display:flex;align-items:center;gap:11px;min-height:44px;padding:11px 14px;margin-bottom:5px;color:#6c7489;border-radius:9px;font-size:12px;font-weight:500;transition:background .15s}.nav-item.active{color:var(--gx-accent);background:var(--gx-accent-soft);font-weight:700}.nav-item:hover{background:#f5f5fc;color:var(--gx-accent)}.nav-ai{margin-left:auto;font-size:8px;padding:1px 5px;line-height:16px;border:1px solid #d6d0ff;border-radius:4px;color:var(--gx-accent)}.sidebar-bottom{margin-top:auto}.study-note{padding:18px 15px;border-radius:12px;background:linear-gradient(140deg,#f2f0ff,#f8f8fd);border:1px solid #eae7ff}.study-icon{display:grid;place-items:center;width:33px;height:33px;border-radius:10px;background:#e3deff;color:var(--gx-accent);margin-bottom:12px}.study-note strong{font-size:12px}.study-note p{font-size:10px;color:var(--gx-text-muted);margin:6px 0 14px}.study-note a{display:flex;align-items:center;justify-content:space-between;font-size:11px;font-weight:600}.sidebar-logout{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;padding:12px 14px;margin-top:14px;border:0;background:none;color:var(--gx-text-muted);font-size:12px}.layout-main-container{margin-left:280px;padding:112px 30px 0 12px;transition:margin-left .2s}.layout-main{max-width:1440px;margin:auto;min-width:0}.layout-main:focus{outline:none}.layout-footer{display:flex;justify-content:space-between;gap:16px;font-size:10px;color:#9198aa;padding:28px 2px 22px;max-width:1440px;margin:auto}.layout-footer a{color:inherit}.layout-static-inactive .layout-sidebar{transform:translateX(-280px)}.layout-static-inactive .layout-main-container{margin-left:20px}.mobile-sidebar-head{display:none}.layout-mask{position:fixed;inset:84px 0 0;background:#20263b66;backdrop-filter:blur(3px);z-index:35;border:0}.skip-link{position:fixed;top:-80px;left:20px;z-index:100;padding:10px 15px;background:var(--gx-accent);color:white;border-radius:8px}.skip-link:focus{top:12px}
@media(max-width:991px){.layout-topbar{height:72px;padding:0 20px;gap:15px}.brand{flex-basis:auto;margin-right:auto}.brand strong{font-size:19px}.breadcrumb{display:none}.topbar-right{margin-left:0;gap:12px}.ai-label{display:none}.account-name{display:none}.menu-toggle{order:-1}.layout-sidebar{top:88px;left:16px;bottom:16px;width:250px;transform:translateX(-285px)}.layout-mobile-active .layout-sidebar{transform:translateX(0)}.layout-main-container,.layout-static-inactive .layout-main-container{margin-left:0;padding:94px 22px 0}.layout-mask{top:72px}.mobile-sidebar-head{display:flex;align-items:center;justify-content:space-between;padding:0 8px 8px;font-weight:600}.layout-footer{padding-top:22px}.nav-item{font-size:13px}}
@media(max-width:480px){.layout-topbar{padding:0 14px;gap:11px}.brand-icon{width:34px;height:34px;border-radius:10px}.brand{gap:8px}.brand strong{font-size:17px}.brand small{font-size:8px;letter-spacing:1px}.avatar{width:30px;height:30px}.account-menu summary{gap:5px}.layout-main-container,.layout-static-inactive .layout-main-container{padding:92px 14px 0}.layout-footer{font-size:9px}}
</style>
