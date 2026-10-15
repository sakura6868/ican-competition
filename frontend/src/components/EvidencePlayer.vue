<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { EyeOff, SkipForward } from "lucide-vue-next";

import type { StepJudgement } from "@/api/types";
import { evidenceTone, resultLabel, seekSecondsForStep } from "@/services/evidence";

const props = defineProps<{
  videoSrc?: string | null;
  steps: StepJudgement[];
  activeStepIndex?: number | null;
}>();

const emit = defineEmits<{
  select: [step: StepJudgement];
}>();

const video = ref<HTMLVideoElement | null>(null);
const durationSeconds = ref(0);
const selectedIndex = ref<number | null>(props.activeStepIndex ?? null);

const timelineDurationMs = computed(() => {
  const evidenceEnd = Math.max(0, ...props.steps.map((step) => step.end_ms));
  return Math.max(evidenceEnd, durationSeconds.value * 1000, 1);
});

function segmentStyle(step: StepJudgement): Record<string, string> {
  const start = Math.max(0, step.start_ms);
  const end = Math.max(start + 500, step.end_ms);
  const left = (start / timelineDurationMs.value) * 100;
  const width = Math.max(2.5, ((end - start) / timelineDurationMs.value) * 100);
  return { left: `${Math.min(97.5, left)}%`, width: `${Math.min(100 - left, width)}%` };
}

function selectStep(step: StepJudgement) {
  selectedIndex.value = step.step_index;
  if (video.value && props.videoSrc) {
    video.value.currentTime = seekSecondsForStep(step);
    void video.value.play().catch(() => undefined);
  }
  emit("select", step);
}

watch(
  () => props.activeStepIndex,
  (index) => {
    if (index == null) return;
    const step = props.steps.find((candidate) => candidate.step_index === index);
    if (step) selectStep(step);
  },
);
</script>

<template>
  <section class="evidence-player" aria-label="步骤证据播放器">
    <div class="viewport">
      <video
        v-if="videoSrc"
        ref="video"
        class="video"
        :src="videoSrc"
        controls
        preload="metadata"
        playsinline
        @loadedmetadata="durationSeconds = video?.duration || 0"
      ></video>
      <div v-else class="no-video">
        <EyeOff :size="30" aria-hidden="true" />
        <strong>没有可播放的视频证据</strong>
        <span>仍可查看系统保存的时间区间与判断依据。</span>
      </div>
    </div>

    <div class="timeline" aria-label="步骤时间轴">
      <div class="timeline-track">
        <button
          v-for="step in steps"
          :key="`${step.step_id}-${step.step_index}`"
          type="button"
          class="timeline-segment"
          :class="[
            `timeline-segment--${evidenceTone(step.judgement)}`,
            { active: selectedIndex === step.step_index },
          ]"
          :style="segmentStyle(step)"
          :aria-label="`跳转到步骤 ${step.step_index}，${resultLabel(step.judgement)}`"
          :title="`步骤 ${step.step_index} · ${resultLabel(step.judgement)}`"
          @click="selectStep(step)"
        >
          {{ step.step_index }}
        </button>
      </div>
      <div class="timeline-scale">
        <span>0:00</span>
        <span>{{ Math.round(timelineDurationMs / 1000) }}s</span>
      </div>
    </div>

    <div class="evidence-list">
      <button
        v-for="step in steps"
        :key="`evidence-${step.step_id}-${step.step_index}`"
        type="button"
        class="evidence-row"
        :class="{ active: selectedIndex === step.step_index }"
        @click="selectStep(step)"
      >
        <span class="step-number">{{ String(step.step_index).padStart(2, "0") }}</span>
        <span class="evidence-copy">
          <strong>{{ resultLabel(step.judgement) }}</strong>
          <span>{{ step.reason || "没有附加判断说明" }}</span>
          <span class="facts" v-if="step.detected_tools?.length || step.detected_objects?.length || step.detected_actions?.length">
            <span v-if="step.detected_tools?.length">工具 {{ step.detected_tools.join("、") }}</span>
            <span v-if="step.detected_objects?.length">对象 {{ step.detected_objects.join("、") }}</span>
            <span v-if="step.detected_actions?.length">动作 {{ step.detected_actions.join("、") }}</span>
          </span>
          <span class="facts" v-if="step.confidence != null || step.keyframe_ref">
            <span v-if="step.confidence != null">置信度 {{ Math.round(step.confidence * 100) }}%</span>
            <span v-if="step.evidence_timestamp_ms != null">
              主证据 {{ (step.evidence_timestamp_ms / 1000).toFixed(1) }}s
            </span>
          </span>
          <img
            v-if="step.evidence_url && selectedIndex === step.step_index"
            class="evidence-thumbnail"
            :src="step.evidence_url"
            :alt="`步骤 ${step.step_index} 的主要证据画面`"
            loading="lazy"
          />
        </span>
        <span class="time-code">
          {{ (step.start_ms / 1000).toFixed(1) }}–{{ (step.end_ms / 1000).toFixed(1) }}s
        </span>
        <SkipForward :size="17" aria-hidden="true" />
      </button>
    </div>
  </section>
</template>

<style scoped>
.evidence-player {
  display: grid;
  gap: 14px;
}

.viewport {
  width: 100%;
  aspect-ratio: 16 / 9;
  min-height: 220px;
  background: #090a0c;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: inset 0 0 36px rgba(0, 0, 0, 0.72);
}

.video {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #090a0c;
}

.no-video {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--gx-text-muted);
  text-align: center;
  padding: 24px;
}

.no-video strong { color: var(--gx-text); font-size: 14px; }
.no-video span { font-size: 12px; }

.timeline { padding: 0 4px; }
.timeline-track {
  position: relative;
  height: 48px;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 7px;
  background: repeating-linear-gradient(90deg, var(--gx-panel-lo) 0, var(--gx-panel-lo) calc(10% - 1px), rgba(255,255,255,.08) 10%);
  overflow: hidden;
}

.timeline-segment {
  position: absolute;
  top: 2px;
  height: 44px;
  min-width: 32px;
  border: 1px solid currentColor;
  border-radius: 5px;
  color: var(--gx-text-muted);
  background: rgba(90, 96, 104, .35);
  cursor: pointer;
  font: 700 11px var(--gx-font-mono);
  transition: filter 160ms ease, transform 160ms ease;
}
.timeline-segment:hover,
.timeline-segment.active { filter: brightness(1.35); transform: translateY(-1px); }
.timeline-segment--pass { color: var(--gx-led-green); background: rgba(61, 220, 132, .24); }
.timeline-segment--fail { color: var(--gx-led-red); background: rgba(255, 92, 92, .24); }
.timeline-segment--inconclusive { color: var(--gx-led-amber); background: rgba(255, 181, 71, .22); }

.timeline-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 5px;
  color: var(--gx-text-muted);
  font: 10px var(--gx-font-mono);
}

.evidence-list { display: grid; gap: 6px; }
.evidence-row {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr) auto 22px;
  gap: 10px;
  align-items: center;
  width: 100%;
  min-height: 58px;
  padding: 8px 10px;
  color: var(--gx-text);
  text-align: left;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 7px;
  background: var(--gx-panel-lo);
  cursor: pointer;
}
.evidence-row:hover,
.evidence-row.active { border-color: var(--gx-accent); background: rgba(255, 181, 71, .07); }
.step-number { color: var(--gx-accent); font: 700 18px var(--gx-font-display); }
.evidence-copy { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.evidence-copy strong { font-size: 12px; }
.evidence-copy > span { color: var(--gx-text-muted); font-size: 12px; overflow-wrap: anywhere; }
.facts { display: flex; gap: 10px; flex-wrap: wrap; font-family: var(--gx-font-mono); }
.evidence-thumbnail {
  width: min(240px, 100%);
  margin-top: 6px;
  aspect-ratio: 16 / 9;
  object-fit: contain;
  border: 1px solid var(--gx-panel-edge);
  border-radius: 6px;
  background: #090a0c;
}
.time-code { color: var(--gx-text-muted); font: 11px var(--gx-font-mono); white-space: nowrap; }

@media (max-width: 600px) {
  .viewport { min-height: 170px; }
  .evidence-row { grid-template-columns: 34px minmax(0, 1fr) 20px; }
  .time-code { grid-column: 2; }
}
</style>
