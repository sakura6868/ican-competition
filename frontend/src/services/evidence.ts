import type { StepJudgement } from "@/api/types";

export function seekSecondsForStep(
  step: Pick<StepJudgement, "start_ms" | "evidence_timestamp_ms">,
): number {
  const timestampMs = step.evidence_timestamp_ms ?? step.start_ms;
  return Math.max(0, timestampMs / 1000 - 1);
}

export function resultLabel(value: string): string {
  if (value === "pass" || value === "passed") return "通过";
  if (value === "fail" || value === "failed") return "未通过";
  if (value === "insufficient_evidence" || value === "unobservable" || value === "inconclusive") {
    return "无法判断";
  }
  return "待分析";
}

export function evidenceTone(value: string): "pass" | "fail" | "inconclusive" | "pending" {
  if (value === "pass" || value === "passed") return "pass";
  if (value === "fail" || value === "failed") return "fail";
  if (value === "insufficient_evidence" || value === "unobservable" || value === "inconclusive") {
    return "inconclusive";
  }
  return "pending";
}
