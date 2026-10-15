import { describe, expect, it } from "vitest";

import type { AssessmentProgress, JobDetail } from "@/api/types";
import { flowState, jobProgress, resumableJobId } from "@/services/assessmentFlow";
import { evidenceTone, resultLabel, seekSecondsForStep } from "@/services/evidence";

function job(status: string, progress = 40): JobDetail {
  return { id: "j1", job_type: "recorded_assessment", status, stage: "vision_analyzing", progress_percent: progress, status_text: "分析中", error_summary: "", retry_count: 0, max_retries: 3, result: {}, checkpoint: {}, created_at: "", started_at: null, finished_at: null };
}

function progress(status: string, ready = false): AssessmentProgress {
  return { session_id: "s1", session_status: status, job: job(status), report_ready: ready, report_id: null };
}

describe("考评流程状态", () => {
  it("映射排队、执行、失败与完成状态", () => {
    expect(flowState(progress("queued"))).toBe("queued");
    expect(flowState(progress("running"))).toBe("running");
    expect(flowState(progress("failed"))).toBe("failed");
    expect(flowState(progress("succeeded", true))).toBe("completed");
    const cancelled = progress("queued");
    cancelled.session_status = "cancelled";
    cancelled.job = null;
    expect(flowState(cancelled)).toBe("failed");
  });

  it("只恢复当前会话自己的任务检查点", () => {
    expect(resumableJobId("session-a", "job-api", "session-b", "job-stale")).toBe("job-api");
    expect(resumableJobId("session-a", null, "session-a", "job-local")).toBe("job-local");
    expect(resumableJobId("session-a", null, "session-b", "job-stale")).toBeUndefined();
  });

  it("限制异常进度值", () => {
    expect(jobProgress(job("running", 130))).toBe(100);
    expect(jobProgress(job("running", -2))).toBe(0);
  });

  it("无法判断不会显示为通过", () => {
    expect(resultLabel("inconclusive")).toBe("无法判断");
    expect(evidenceTone("insufficient_evidence")).toBe("inconclusive");
  });

  it("证据跳转提前一秒且不早于零", () => {
    expect(seekSecondsForStep({ start_ms: 5400 })).toBe(4.4);
    expect(seekSecondsForStep({ start_ms: 300 })).toBe(0);
    expect(seekSecondsForStep({ start_ms: 5400, evidence_timestamp_ms: 8200 })).toBeCloseTo(7.2);
  });
});
