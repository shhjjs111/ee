// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ContextBreakdown } from "../types";
import { ContextBreakdownView } from "./ContextPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

function render(node: React.ReactNode) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  act(() => createRoot(el).render(node));
  return el;
}

const data: ContextBreakdown = {
  items: [
    { key: "system", label: "系统提示", tokens: 1200, percent: 10 },
    { key: "tools", label: "工具定义", tokens: 3000, percent: 25 },
    { key: "history", label: "历史消息", tokens: 7800, percent: 65 },
  ],
  tools: [{ name: "shell", tokens: 900 }, { name: "read", tokens: 300 }],
  total: 12000, window: 1_000_000, used: 12500, actual: true, images: 0, messages: 5,
  savings: { pruned: 4000, prunes: 1, pruned_outputs: 2, compacted: 0 },
  cache: { hit: 500, hit_rate: 0.5, hit_coverage: 0.8 },
  compactions: 0, frozen: true,
  thresholds: { compact: 0.8, prune: 0.6 }, model: "m", estimated: true,
};

describe("ContextBreakdownView", () => {
  it("显示各部分 token / 占比、节省与阈值，展开工具明细，点压缩回调", () => {
    const onCompact = vi.fn();
    const el = render(<ContextBreakdownView data={data} onCompact={onCompact} canCompact />);
    expect(el.textContent).toContain("上下文构成");
    expect(el.textContent).toContain("已用 13k / 1.0M（1%）");
    const hist = el.querySelector('[data-key="history"]')!;
    expect(hist.textContent).toContain("历史消息");
    expect(hist.textContent).toContain("7.8k");
    expect(hist.textContent).toContain("65%");
    expect(el.textContent).toContain("修剪旧工具输出省 ~4.0k");
    expect(el.textContent).toContain("服务端缓存命中 50% · 覆盖 80% 输入");
    expect(el.textContent).toContain("80% 时自动压缩");
    expect(el.textContent).not.toContain("shell");
    const more = [...el.querySelectorAll("button")].find((b) => b.textContent === "2 个")!;
    act(() => more.click());
    expect(el.textContent).toContain("shell");
    const btn = [...el.querySelectorAll("button")].find((b) => b.textContent?.includes("立即压缩"))!;
    act(() => btn.click());
    expect(onCompact).toHaveBeenCalledOnce();
  });
});
