import { describe, expect, it } from "vitest";

import { demoReplayPath, parseDemoScenario, rejectedRecommendation } from "@/services/demoReplay";

describe("答辩工程回放", () => {
  it("只接受后端支持的三个固定场景", () => {
    expect(parseDemoScenario("complete")).toBe("complete");
    expect(parseDemoScenario("error")).toBe("error");
    expect(parseDemoScenario("inconclusive")).toBe("inconclusive");
    expect(parseDemoScenario("pass")).toBeNull();
    expect(parseDemoScenario(["complete"])).toBeNull();
  });

  it("构造经过认证的回放 API 路径", () => {
    expect(demoReplayPath("error")).toBe("/demo/load/error");
  });

  it("拒判建议不会误写为全部符合", () => {
    const recommendation = rejectedRecommendation("画面严重遮挡");
    expect(recommendation).toContain("画面严重遮挡");
    expect(recommendation).toContain("重新提交");
    expect(recommendation).not.toContain("均符合");
  });
});
