import { describe, expect, it } from "vitest";
import type { ContextBreakdown } from "../types";
import { cacheLine, cacheQualityLine, fmtTokens, savingsLine, thresholdLine, usageLine } from "./context";

const bd = (over: Partial<ContextBreakdown> = {}): ContextBreakdown => ({
  items: [], tools: [], total: 0, window: 1_000_000, used: 12_300, actual: true, images: 0, messages: 0,
  savings: { pruned: 0, prunes: 0, pruned_outputs: 0, compacted: 0 },
  cache: { hit: 0, hit_rate: null, hit_coverage: null },
  compactions: 0, frozen: true,
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
    expect(thresholdLine(bd({ thresholds: { compact: 0.8, prune: 0.6, prune_tokens: 150_000 } }))).toBe(
      "150k token 时修剪旧工具输出，80% 时自动压缩",
    );
    // 消息级修剪默认关（prune_tokens=null）：不能再说“60% 时修剪”，否则用户会以为它还会动手
    expect(thresholdLine(bd({ thresholds: { compact: 0.8, prune: 0.6, prune_tokens: null } }))).toBe("80% 时自动压缩");
    expect(thresholdLine(bd({ thresholds: { compact: 0.8, prune: 0.6, prune_tokens: 0 } }))).toBe("80% 时自动压缩");
    expect(thresholdLine(bd({ thresholds: { compact: null, prune: null } }))).toBeNull();
  });
  it("旧推理节省单独计，不与工具修剪合并", () => {
    // 旧实现把清推理的节省量写进 pruned，开着清推理时界面会把工具修剪的数字说大
    expect(savingsLine(bd({ savings: { pruned: 0, prunes: 0, pruned_outputs: 0, compacted: 0, reasoning_cut: 12_000 } }))).toBe(
      "旧推理不再回传省 ~12k",
    );
    expect(
      savingsLine(bd({ savings: { pruned: 2000, prunes: 1, pruned_outputs: 3, compacted: 0, reasoning_cut: 12_000 } })),
    ).toBe("旧推理不再回传省 ~12k · 修剪旧工具输出省 ~2.0k");
  });

  it("前缀稳定率与尾块口径来自本地账本，旧数据不显示", () => {
    // 旧账本没有指纹 / 尾块字段：什么都不加，不能编造成 0%。
    expect(cacheQualityLine(bd())).toBeNull();
    expect(
      cacheQualityLine(
        bd({
          cache: { hit: 0, hit_rate: 0.98, hit_coverage: 1, prefix_rate: 0.97, prefix_observed: 120, tail_rate: 0.99, tail_tokens: 1234 },
        }),
      ),
    ).toBe("客户端前缀稳定 97%（120 次） · 扣掉尾块 1.2k 后命中 99%");
    // 只有前缀（还没有尾块数据）：只说前缀。
    expect(cacheQualityLine(bd({ cache: { hit: 0, hit_rate: 0.98, hit_coverage: 1, prefix_rate: 1, prefix_observed: 5 } }))).toBe(
      "客户端前缀稳定 100%（5 次）",
    );
    // 只有尾块（旧会话中段开始记）：只说尾块。
    expect(cacheQualityLine(bd({ cache: { hit: 0, hit_rate: 0.98, hit_coverage: 1, tail_rate: 0.995, tail_tokens: 88 } }))).toBe(
      "扣掉尾块 88 后命中 100%",
    );
  });

  it("缓存命中带覆盖率，未知不显示 0", () => {
    // 有命中字段：显示命中率 + 覆盖率
    expect(cacheLine(bd({ cache: { hit: 500, hit_rate: 0.5, hit_coverage: 0.8 } }))).toBe(
      "服务端缓存命中 50% · 覆盖 80% 输入",
    );
    // 无命中字段：未知，不显示 0
    expect(cacheLine(bd({ cache: { hit: 0, hit_rate: null, hit_coverage: null } }))).toBeNull();
    // 覆盖率缺失但命中率有（老数据）：只显示命中率，不编造覆盖率
    expect(cacheLine(bd({ cache: { hit: 100, hit_rate: 0.3, hit_coverage: null } }))).toBe(
      "服务端缓存命中 30%",
    );
  });

  it("有主对话口径时优先用它", () => {
    // 侧路请求（标题 / commit / 压缩 / 子代理）命中恒 0，总口径会被拉低；
    // 用户关心的是主对话到底命中多少。
    expect(
      cacheLine(
        bd({
          cache: { hit: 900, hit_rate: 0.9, hit_coverage: 1, main_hit_rate: 0.99, main_hit_coverage: 1, main_requests: 8 },
        }),
      ),
    ).toBe("主对话缓存命中 99% · 覆盖 100% 输入");
    // 主对话还没有样本（main_hit_rate=null）：回落到总口径，不要显示 0%
    expect(cacheLine(bd({ cache: { hit: 500, hit_rate: 0.5, hit_coverage: 0.8, main_hit_rate: null } }))).toBe(
      "服务端缓存命中 50% · 覆盖 80% 输入",
    );
    // 主对话覆盖率未知：只说命中率
    expect(cacheLine(bd({ cache: { hit: 0, hit_rate: null, hit_coverage: null, main_hit_rate: 0.97 } }))).toBe(
      "主对话缓存命中 97%",
    );
  });

  it("展示冷启动和已知原因，不把它们塞进命中率", () => {
    expect(
      cacheLine(
        bd({
          cache: {
            hit: 500, hit_rate: 0.5, hit_coverage: 0.8, cold: 2,
            causes: { model_switch: 1, compact: 2 },
          },
        }),
      ),
    ).toBe("服务端缓存命中 50% · 覆盖 80% 输入 · 冷启动 2 次 · 已知原因 compact 2、model_switch 1");
  });

  it("老账本里分不清的行单独说，不冒充冷启动", () => {
    expect(
      cacheLine(
        bd({
          cache: {
            hit: 500, hit_rate: 0.5, hit_coverage: 0.8, cold: 1, legacy: 3,
          },
        }),
      ),
    ).toBe("服务端缓存命中 50% · 覆盖 80% 输入 · 冷启动 1 次 · 旧账本无法归因 3 次");
  });

  it("空闲过期与冷启动分开说", () => {
    // 前者是服务端 TTL 到期，后者是进程/会话刚起；都排除在「前缀不稳」之外，但原因不同。
    expect(
      cacheLine(
        bd({ cache: { hit: 500, hit_rate: 0.5, hit_coverage: 0.8, cold: 1, cold_ttl: 2 } }),
      ),
    ).toBe("服务端缓存命中 50% · 覆盖 80% 输入 · 冷启动 1 次 · 空闲过期 2 次");
  });

  it("缺用量的请求要单独说出来", () => {
    // 缺用量的行 prompt=0，token 覆盖率永远是 100%；不点明的话那行
    // 「覆盖 100% 输入」会把「有请求根本没报用量」盖掉。
    expect(
      cacheLine(
        bd({
          cache: {
            hit: 900, hit_rate: 0.9, hit_coverage: 1,
            main_hit_rate: 0.99, main_hit_coverage: 1, main_requests: 3, usage_missing: 2,
          },
        }),
      ),
    ).toBe("主对话缓存命中 99% · 覆盖 100% 输入 · 缺用量 2 次");
    // usage_missing=0 不显示多余的一段
    expect(
      cacheLine(bd({ cache: { hit: 900, hit_rate: 0.9, hit_coverage: 1, main_hit_rate: 0.99, main_hit_coverage: 1, usage_missing: 0 } })),
    ).toBe("主对话缓存命中 99% · 覆盖 100% 输入");
  });
});
