<!-- Adapted from frappe/lms frontend/src/components/CourseCard.vue, AGPL-3.0.
     Upstream: https://github.com/frappe/lms. See LICENSES/frappe-lms-AGPL-3.0.txt.
     Changes: SOP props, local CSS, real practice progress, original cover artwork,
     removed Frappe resources, enrollment/rating/instructor fields and UI dependencies. -->
<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, type RouteLocationRaw } from "vue-router";
import { ArrowRight, BookOpen, Check, Layers, Sparkles } from "lucide-vue-next";
const props = withDefaults(defineProps<{
  course: { title: string; lessons: number; version: number; ready: boolean; progress?: number | null };
  to?: RouteLocationRaw;
  selectable?: boolean;
  selected?: boolean;
  actionLabel?: string;
}>(), { selectable: false, selected: false, actionLabel: "开始学习" });
defineEmits<{ select: [] }>();
const tone = computed(() => ["violet", "mint", "peach"][Array.from(props.course.title).reduce((sum, char) => sum + char.charCodeAt(0), 0) % 3]);
const progress = computed(() => props.course.progress == null ? null : Math.max(0, Math.min(100, Math.round(props.course.progress))));
</script>
<template>
  <article class="course-card" :class="[tone, { selected }]">
    <div class="course-cover" aria-hidden="true">
      <span class="cover-label"><Layers :size="12" />SOP 实训课程</span>
      <div class="cover-art"><div class="art-sheet"><span></span><span></span><span></span></div><div class="art-play"><BookOpen :size="29" :stroke-width="1.6" /></div><div class="art-check"><Check :size="16" /></div><i class="art-orbit"></i></div>
      <span v-if="selected" class="selected-label"><Check :size="13" />已选择</span>
    </div>
    <div class="course-body">
      <div class="course-meta"><span><BookOpen :size="13" />{{ course.lessons }} 个学习步骤</span><span>v{{ course.version }}</span></div>
      <h3>{{ course.title }}</h3>
      <p class="short-introduction">{{ course.ready ? '跟随标准示范，逐步练习，获得针对性的操作指导。' : '教师正在完善示范片段与完成条件，准备好后即可练习。' }}</p>
      <div v-if="progress !== null" class="course-progress"><div><span>学习进度</span><strong>{{ progress }}%</strong></div><div class="progress-track" role="progressbar" :aria-label="`${course.title}学习进度`" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${progress}%` }"></span></div></div>
      <div class="course-footer"><span class="course-status" :class="{ ready: course.ready }"><Sparkles :size="12" />{{ course.ready ? 'AI 陪练' : '待教师准备' }}</span><button v-if="selectable" class="course-action" type="button" :aria-pressed="selected" :aria-label="`选择课程：${course.title}`" @click="$emit('select')">{{ selected ? '已选择' : '选择课程' }}<ArrowRight :size="14" /></button><RouterLink v-else-if="to" :to="to" class="course-action">{{ actionLabel }}<ArrowRight :size="14" /></RouterLink></div>
    </div>
  </article>
</template>
<style scoped>
.course-card{display:flex;flex-direction:column;min-width:0;height:100%;border:1px solid var(--gx-border);border-radius:14px;overflow:hidden;background:white;transition:border-color .15s,box-shadow .15s}.course-card:hover{box-shadow:0 8px 24px #20263b08;border-color:#d4d0ed}.course-card.selected{border-color:var(--gx-accent);box-shadow:0 0 0 2px #6055d815}.course-cover{--cover-bg:#eeeafb;--cover-ink:#7363cd;position:relative;overflow:hidden;height:142px;background:var(--cover-bg);color:var(--cover-ink)}.mint .course-cover{--cover-bg:#e7f2ee;--cover-ink:#469c85}.peach .course-cover{--cover-bg:#fbede2;--cover-ink:#c58b5d}.cover-label{position:absolute;top:16px;left:17px;display:flex;gap:5px;align-items:center;font-size:9px;letter-spacing:1px}.cover-art{position:absolute;width:200px;height:120px;left:50%;top:25px;transform:translateX(-50%)}.art-sheet{position:absolute;left:27px;top:12px;width:90px;height:104px;border-radius:10px;transform:rotate(-12deg);background:#ffffffa8;border:2px solid #fff;box-shadow:0 6px 10px #20263b06;padding:25px 13px}.art-sheet span{display:block;height:5px;background:var(--cover-ink);opacity:.3;margin-bottom:10px;border-radius:3px}.art-sheet span:nth-child(2){width:80%}.art-sheet span:nth-child(3){width:60%}.art-play{position:absolute;z-index:2;left:93px;top:27px;display:grid;place-items:center;width:73px;height:66px;border-radius:16px;background:var(--cover-ink);color:white;transform:rotate(8deg);box-shadow:0 8px 15px #20263b0b}.art-check{position:absolute;z-index:3;left:157px;top:80px;display:grid;place-items:center;width:27px;height:27px;border-radius:50%;background:white;color:var(--cover-ink);box-shadow:0 3px 10px #20263b10}.art-orbit{position:absolute;left:14px;top:48px;width:165px;height:53px;border:1px dashed currentColor;border-radius:50%;opacity:.2;transform:rotate(-28deg)}.selected-label{position:absolute;right:12px;top:12px;display:flex;gap:4px;align-items:center;font-size:9px;background:white;padding:4px 8px;border-radius:6px;color:var(--gx-accent)}.course-body{display:flex;flex:1;flex-direction:column;padding:17px}.course-meta{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:10px;color:var(--gx-text-muted)}.course-meta>span:first-child{display:flex;align-items:center;gap:5px}.course-body h3{margin:12px 0 7px;font-size:14px;font-weight:650;line-height:1.6;overflow-wrap:anywhere}.short-introduction{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;text-overflow:ellipsis;margin:0 0 18px;font-size:11px;line-height:1.8;color:var(--gx-text-muted)}.course-progress{margin-bottom:16px}.course-progress>div:first-child{display:flex;justify-content:space-between;font-size:10px;color:var(--gx-text-muted);margin-bottom:7px}.course-progress strong{font-weight:600;color:var(--gx-accent)}.progress-track{height:5px;overflow:hidden;border-radius:10px;background:var(--gx-panel-lo)}.progress-track span{display:block;height:100%;background:var(--gx-accent);border-radius:10px}.course-footer{margin-top:auto;display:flex;justify-content:space-between;align-items:center;gap:8px;padding-top:13px;border-top:1px solid #f0f1f6}.course-status{display:flex;align-items:center;gap:5px;font-size:9px;color:var(--gx-warning)}.course-status.ready{color:var(--gx-accent)}.course-action{display:flex;align-items:center;gap:5px;min-height:44px;border:0;background:transparent;padding:3px 0;font-size:11px;color:var(--gx-accent);font-weight:600}.course-action:hover{text-decoration:underline}
</style>
