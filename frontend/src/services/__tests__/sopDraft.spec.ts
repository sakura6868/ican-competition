import { describe, expect, it } from "vitest";

import type { SopStep } from "@/api/types";
import { duplicateStep, moveStep, normalizeWeights, removeStep, splitTerms, validateDraft } from "@/services/sopDraft";

function step(index: number, weight: number, predecessors: number[] = []): SopStep {
  return {
    id: `step-${index}`,
    index,
    name: `步骤 ${index}`,
    predecessors,
    optional: false,
    critical: index === 2,
    tools: ["tool"],
    objects: ["part"],
    actions: ["operate"],
    expected_duration_s: 20,
    observable_checks: ["动作可见"],
    unobservable_checks: [],
    weight,
    evidence_refs: [{ kind: "video", ref: "00:01-00:02", description: "测试证据", asset_id: "video-1" }],
  };
}

describe("SOP 草稿编辑", () => {
  it("解析词条并去重", () => {
    expect(splitTerms("扳手， 螺丝刀,扳手\n砂轮")).toEqual(["扳手", "螺丝刀", "砂轮"]);
  });

  it("归一权重后严格等于 100", () => {
    const normalized = normalizeWeights([step(1, 1), step(2, 2, [1]), step(3, 3, [2])]);
    expect(normalized.reduce((sum, item) => sum + item.weight, 0)).toBe(100);
  });

  it("移动步骤时按稳定 id 重映射依赖", () => {
    const moved = moveStep([step(1, 30), step(2, 30, [1]), step(3, 40, [2])], 2, 1);
    expect(moved.map((item) => item.id)).toEqual(["step-1", "step-3", "step-2"]);
    expect(moved.find((item) => item.id === "step-3")?.predecessors).toEqual([3]);
  });

  it("删除步骤时清除指向它的依赖", () => {
    const result = removeStep([step(1, 30), step(2, 30, [1]), step(3, 40, [2])], 1);
    expect(result.map((item) => item.index)).toEqual([1, 2]);
    expect(result[1].predecessors).toEqual([]);
  });

  it("复制步骤保留后续依赖并让副本依赖原步骤", () => {
    const result = duplicateStep([step(1, 50), step(2, 50, [1])], 0, "copy");
    expect(result.map((item) => item.id)).toEqual(["step-1", "copy", "step-2"]);
    expect(result[1].predecessors).toEqual([1]);
    expect(result[2].predecessors).toEqual([1]);
  });

  it("拒绝总权重错误和缺少动作的草稿", () => {
    const invalid = [step(1, 60), step(2, 30, [1])];
    invalid[1].actions = [];
    expect(validateDraft(invalid).map((issue) => issue.message)).toEqual([
      "步骤 2 缺少动作词",
      "步骤总权重应为 100，当前为 90.00",
    ]);
  });

  it("手册支撑等级要求真实手册索引来源", () => {
    const draft = [step(1, 100)];
    expect(validateDraft(draft, "manual_backed").map((issue) => issue.message)).toContain(
      "手册支撑等级至少需要一条真实手册证据",
    );
    draft[0].evidence_refs.push({
      kind: "manual",
      ref: "manual@manual-1#chunk=7;page=3",
      description: "安全手册",
      asset_id: "manual-1",
      chunk_id: 7,
      chunk_index: 1,
      content_hash: "a".repeat(64),
      page: 3,
      section: "断电操作",
    });
    expect(validateDraft(draft, "manual_backed")).toEqual([]);
  });

  it("传感器等级拒绝没有 sensor# 定位符的证据", () => {
    const draft = [step(1, 100)];
    draft[0].evidence_refs.push({ kind: "sensor", ref: "cal-1", description: "扭矩记录" });
    expect(validateDraft(draft, "sensor_verified").map((issue) => issue.message)).toContain(
      "步骤 1 的传感器证据必须以 sensor# 开头",
    );
  });
});
