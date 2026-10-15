import { describe, expect, it } from "vitest";

import {
  isActiveJobStatus,
  isTerminalJobStatus,
  pollingRetryDelayMs,
} from "@/services/jobPolling";

describe("异步任务轮询", () => {
  it("短暂失败按 1、2、4、8 秒退避并封顶", () => {
    expect([1, 2, 3, 4, 9].map(pollingRetryDelayMs)).toEqual([1000, 2000, 4000, 8000, 8000]);
  });

  it("异常重试次数不会产生零或负延迟", () => {
    expect(pollingRetryDelayMs(0)).toBe(1000);
    expect(pollingRetryDelayMs(-4)).toBe(1000);
    expect(pollingRetryDelayMs(Number.NaN)).toBe(1000);
  });

  it("区分活动状态和终态", () => {
    expect(["queued", "running", "retrying"].every(isActiveJobStatus)).toBe(true);
    expect(["succeeded", "failed", "cancelled"].every(isTerminalJobStatus)).toBe(true);
    expect(isActiveJobStatus("failed")).toBe(false);
    expect(isTerminalJobStatus("running")).toBe(false);
  });
});
