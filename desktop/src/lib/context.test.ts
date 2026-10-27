import { describe, expect, it } from "vitest";
import type { ContextBreakdown } from "../types";
import { fmtTokens, savingsLine, thresholdLine, usageLine } from "./context";

const bd = (over: Partial<ContextBreakdown> = {}): ContextBreakdown => ({
  items: [], tools: [], total: 0, window: 1_000_000, used: 12_300, actual: true, images: 0, messages: 0,
  savings: { pruned: 0, prunes: 0, pruned_outputs: 0, compacted: 0 }, compactions: 0, frozen: true,
  thresholds: { compact: 0.8, prune: 0.6 }, model: "m", estimated: true, ...over,
});

describe("context", () => {
  it("token 格式", () => {
    expect(fmtTokens(950)).toBe("950");
    expect(fmtTokens(1234)).toBe("1.2k");
    expect(fmtTokens(12_300)).toBe("12k");
    expect(fmtTokens(1_000_000)).toBe("1.0M");
  });
  it("用量与估算标注", () => {
    expect(usageLine(bd())).toBe("已用 12k / 1.0M（1%）");
    expect(usageLine(bd({ actual: false }))).toContain("估算");
  });
  it("节省与阈值", () => {
    expect(savingsLine(bd())).toBeNull();
    expect(savingsLine(bd({ savings: { pruned: 2000, prunes: 1, pruned_outputs: 3, compacted: 50_000 }, compactions: 2 }))).toBe(
      "修剪旧工具输出省 ~2.0k · 压缩 2 次省 ~50k",
    );
    expect(thresholdLine(bd())).toBe("60% 时修剪旧工具输出，80% 时自动压缩");
    expect(thresholdLine(bd({ thresholds: { compact: null, prune: null } }))).toBeNull();
  });
});
