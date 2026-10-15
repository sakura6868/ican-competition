import type { SopStep, SourceGrade } from "@/api/types";

export interface DraftIssue {
  stepIndex: number | null;
  message: string;
}

export function cloneSteps(steps: SopStep[]): SopStep[] {
  return steps.map((step) => ({
    ...step,
    teaching: step.teaching ? { ...step.teaching, reference_timestamps_ms: [...step.teaching.reference_timestamps_ms] } : step.teaching,
    verification: step.verification ? { ...step.verification, checks: step.verification.checks.map(c => ({ ...c })) } : step.verification,
    predecessors: [...step.predecessors],
    tools: [...step.tools],
    objects: [...step.objects],
    actions: [...step.actions],
    completion_checks: [...(step.completion_checks ?? [])],
    observable_checks: [...step.observable_checks],
    unobservable_checks: [...step.unobservable_checks],
    evidence_refs: step.evidence_refs.map((ref) => ({ ...ref })),
  }));
}

export function splitTerms(value: string): string[] {
  return [...new Set(value.split(/[,，\n]/).map((part) => part.trim()).filter(Boolean))];
}

export function moveStep(steps: SopStep[], from: number, to: number): SopStep[] {
  if (from < 0 || to < 0 || from >= steps.length || to >= steps.length || from === to) {
    return cloneSteps(steps);
  }
  const predecessorIds = new Map(
    steps.map((step) => [
      step.id,
      step.predecessors
        .map((index) => steps.find((candidate) => candidate.index === index)?.id)
        .filter((id): id is string => Boolean(id)),
    ]),
  );
  const result = cloneSteps(steps);
  const [moved] = result.splice(from, 1);
  result.splice(to, 0, moved);
  result.forEach((step, index) => {
    step.index = index + 1;
  });
  const indexById = new Map(result.map((step) => [step.id, step.index]));
  result.forEach((step) => {
    step.predecessors = (predecessorIds.get(step.id) ?? [])
      .map((id) => indexById.get(id))
      .filter((index): index is number => index !== undefined)
      .sort((a, b) => a - b);
  });
  return result;
}

export function removeStep(steps: SopStep[], targetIndex: number): SopStep[] {
  if (targetIndex < 0 || targetIndex >= steps.length) return cloneSteps(steps);
  const targetId = steps[targetIndex].id;
  const predecessorIds = new Map(
    steps.map((step) => [
      step.id,
      step.predecessors
        .map((index) => steps.find((candidate) => candidate.index === index)?.id)
        .filter((id): id is string => Boolean(id) && id !== targetId),
    ]),
  );
  const result = cloneSteps(steps.filter((_, index) => index !== targetIndex));
  result.forEach((step, index) => {
    step.index = index + 1;
  });
  const indexById = new Map(result.map((step) => [step.id, step.index]));
  result.forEach((step) => {
    step.predecessors = (predecessorIds.get(step.id) ?? [])
      .map((id) => indexById.get(id))
      .filter((index): index is number => index !== undefined)
      .sort((a, b) => a - b);
  });
  return result;
}

export function duplicateStep(steps: SopStep[], sourceIndex: number, id: string): SopStep[] {
  if (sourceIndex < 0 || sourceIndex >= steps.length) return cloneSteps(steps);
  const predecessorIds = new Map(
    steps.map((step) => [
      step.id,
      step.predecessors
        .map((index) => steps.find((candidate) => candidate.index === index)?.id)
        .filter((candidateId): candidateId is string => Boolean(candidateId)),
    ]),
  );
  const source = steps[sourceIndex];
  const copy = cloneSteps([source])[0];
  copy.id = id;
  copy.name = `${source.name}（副本）`;
  const result = cloneSteps(steps);
  result.splice(sourceIndex + 1, 0, copy);
  result.forEach((step, index) => { step.index = index + 1; });
  const indexById = new Map(result.map((step) => [step.id, step.index]));
  result.forEach((step) => {
    const dependencyIds = step.id === id ? [source.id] : predecessorIds.get(step.id) ?? [];
    step.predecessors = dependencyIds
      .map((candidateId) => indexById.get(candidateId))
      .filter((index): index is number => index !== undefined)
      .sort((a, b) => a - b);
  });
  return result;
}

export function normalizeWeights(steps: SopStep[]): SopStep[] {
  const result = cloneSteps(steps);
  if (!result.length) return result;
  const positive = result.map((step) => Math.max(0, Number(step.weight) || 0));
  const total = positive.reduce((sum, value) => sum + value, 0);
  const raw = total > 0 ? positive.map((value) => (value / total) * 100) : result.map(() => 100 / result.length);
  result.forEach((step, index) => {
    step.weight = Math.round(raw[index] * 100) / 100;
  });
  const roundedTotal = result.reduce((sum, step) => sum + step.weight, 0);
  result[result.length - 1].weight = Math.round((result[result.length - 1].weight + 100 - roundedTotal) * 100) / 100;
  return result;
}

export function validateDraft(
  steps: SopStep[],
  sourceGrade: SourceGrade = "video_reference",
): DraftIssue[] {
  const issues: DraftIssue[] = [];
  if (!steps.length) return [{ stepIndex: null, message: "SOP 至少需要一个步骤" }];
  steps.forEach((step, position) => {
    if (!step.name.trim()) issues.push({ stepIndex: position, message: `步骤 ${position + 1} 缺少名称` });
    if (!step.actions.length) issues.push({ stepIndex: position, message: `步骤 ${position + 1} 缺少动作词` });
    if (!step.completion_checks?.length && !step.observable_checks.length && !step.unobservable_checks.length) {
      issues.push({ stepIndex: position, message: `步骤 ${position + 1} 缺少通过条件` });
    }
    if (!step.evidence_refs.length) {
      issues.push({ stepIndex: position, message: `步骤 ${position + 1} 缺少来源证据` });
    }
    step.evidence_refs.forEach((evidence) => {
      const allowed = sourceGrade === "sensor_verified"
        || (sourceGrade === "manual_backed" && evidence.kind !== "sensor")
        || (sourceGrade === "video_reference" && evidence.kind === "video");
      if (!allowed) {
        issues.push({ stepIndex: position, message: `步骤 ${position + 1} 的 ${evidence.kind} 证据与来源等级不兼容` });
      }
      if (!evidence.ref.trim()) {
        issues.push({ stepIndex: position, message: `步骤 ${position + 1} 存在空来源证据` });
      }
      if (evidence.kind === "video" && !evidence.asset_id) {
        issues.push({ stepIndex: position, message: `步骤 ${position + 1} 的视频证据未绑定资产` });
      }
      if (evidence.kind === "manual" && (!evidence.asset_id || !evidence.chunk_id || !evidence.content_hash)) {
        issues.push({ stepIndex: position, message: `步骤 ${position + 1} 的手册证据缺少资产或索引来源` });
      }
      if (evidence.kind === "sensor" && !evidence.ref.startsWith("sensor#")) {
        issues.push({ stepIndex: position, message: `步骤 ${position + 1} 的传感器证据必须以 sensor# 开头` });
      }
    });
    if (!Number.isFinite(step.weight) || step.weight <= 0) {
      issues.push({ stepIndex: position, message: `步骤 ${position + 1} 权重必须大于 0` });
    }
    if (step.predecessors.some((index) => index >= step.index)) {
      issues.push({ stepIndex: position, message: `步骤 ${position + 1} 的前置步骤必须位于它之前` });
    }
  });
  const total = steps.reduce((sum, step) => sum + (Number(step.weight) || 0), 0);
  if (Math.abs(total - 100) > 0.01) {
    issues.push({ stepIndex: null, message: `步骤总权重应为 100，当前为 ${total.toFixed(2)}` });
  }
  const kinds = new Set(steps.flatMap((step) => step.evidence_refs.map((ref) => ref.kind)));
  if (sourceGrade === "manual_backed" && !kinds.has("manual")) {
    issues.push({ stepIndex: null, message: "手册支撑等级至少需要一条真实手册证据" });
  }
  if (sourceGrade === "sensor_verified" && !kinds.has("sensor")) {
    issues.push({ stepIndex: null, message: "传感器核验等级至少需要一条传感器证据" });
  }
  return issues;
}

export function createBlankStep(index: number): SopStep {
  return {
    id: `step_${Date.now()}_${index}`,
    index,
    name: `新步骤 ${index}`,
    predecessors: index > 1 ? [index - 1] : [],
    optional: false,
    critical: false,
    tools: [],
    objects: [],
    actions: [],
    expected_duration_s: 30,
    completion_checks: [],
    observable_checks: [],
    unobservable_checks: [],
    weight: 1,
    evidence_refs: [],
  };
}
