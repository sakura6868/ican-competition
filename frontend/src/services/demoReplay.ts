import type { DemoScenario } from "@/api/types";

const DEMO_SCENARIOS = new Set<DemoScenario>(["complete", "error", "inconclusive"]);

export function parseDemoScenario(value: unknown): DemoScenario | null {
  if (typeof value !== "string") return null;
  return DEMO_SCENARIOS.has(value as DemoScenario) ? (value as DemoScenario) : null;
}

export function demoReplayPath(scenario: DemoScenario): string {
  return `/demo/load/${scenario}`;
}

export function rejectedRecommendation(reason: string): string {
  const detail = reason.trim() || "关键步骤缺少足够的可见证据";
  return `本次无法形成可靠结论：${detail}。请调整机位、减少遮挡或检查视觉模型后重新提交。`;
}
