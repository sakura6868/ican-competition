import type { AssessmentProgress, JobDetail } from "@/api/types";

export type FlowState = "idle" | "queued" | "running" | "completed" | "failed";

export function flowState(progress: AssessmentProgress | null): FlowState {
  if (!progress) return "idle";
  if (progress.report_ready || progress.session_status === "completed" || progress.job?.status === "succeeded") {
    return "completed";
  }
  if (
    progress.job?.status === "failed"
    || progress.job?.status === "cancelled"
    || progress.session_status === "failed"
    || progress.session_status === "cancelled"
  ) {
    return "failed";
  }
  if (progress.job?.status === "running" || progress.job?.status === "retrying") return "running";
  return "queued";
}

/** Never reuse a task id persisted for a different session opened from a direct link. */
export function resumableJobId(
  targetSessionId: string,
  sessionJobId: string | null | undefined,
  storedSessionId: string | null,
  storedJobId: string | null,
): string | undefined {
  if (sessionJobId) return sessionJobId;
  return storedSessionId === targetSessionId ? storedJobId ?? undefined : undefined;
}

export function jobProgress(job: JobDetail | null): number {
  if (!job) return 0;
  return Math.max(0, Math.min(100, Number(job.progress_percent) || 0));
}
