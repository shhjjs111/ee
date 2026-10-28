// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import type { ThreadSummary } from "../types";
import { ChildThreadCard, terminalText } from "./ChildThreadCard";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: { unmount(): void }[] = [];
afterEach(async () => {
  for (const r of roots.splice(0)) await act(async () => r.unmount());
  document.body.innerHTML = "";
});

function child(over: Partial<ThreadSummary> = {}): ThreadSummary {
  return {
    id: "20261028-120000-child1",
    title: "改个文档",
    cwd: "C:/repo",
    model: "deepseek-flash",
    mode: "default",
    status: "idle",
    orchestrator_parent_id: "20261028-110000-parent",
    orchestrator_role: "child",
    worktree: { path: "C:/wt/one", branch: "ee/agent-one", repo: "C:/repo", name: "one" },
    ...over,
  };
}

async function render(el: React.ReactElement): Promise<HTMLElement> {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(<TT.Provider>{el}</TT.Provider>));
  return host;
}

describe("终态文案", () => {
  it("idle 且没有 run 记录时不能报「完成」——没有证据说明它跑完了", () => {
    expect(terminalText(child())).toBe("空闲");
  });

  it("terminal_reason=budget 显示超预算，不能被 idle 盖成完成", () => {
    const t = child({ runs: [{ id: "run_1", started: 1, status: "done", terminal_reason: "budget" }] });
    expect(terminalText(t)).toBe("超预算停止");
  });

  it("terminal_reason=interrupted 显示已中断（重启清理掉的遗留回合）", () => {
    const t = child({ runs: [{ id: "run_1", started: 1, status: "done", terminal_reason: "interrupted" }] });
    expect(terminalText(t)).toBe("已中断");
  });

  it("completed 才是完成", () => {
    const t = child({ runs: [{ id: "run_1", started: 1, status: "done", terminal_reason: "completed" }] });
    expect(terminalText(t)).toBe("完成");
  });

  it("正在跑时优先显示运行中，不看上一条的终态", () => {
    const t = child({
      status: "running",
      runs: [{ id: "run_2", started: 2, status: "running", terminal_reason: null }],
    });
    expect(terminalText(t)).toBe("运行中");
  });
});

describe("子会话卡片", () => {
  it("显示标题、分支与终态", async () => {
    const host = await render(
      <ChildThreadCard
        t={child({ runs: [{ id: "run_1", started: 1, status: "done", terminal_reason: "budget" }] })}
        onOpen={() => undefined}
      />,
    );
    const text = host.textContent ?? "";
    expect(text).toContain("改个文档");
    expect(text).toContain("ee/agent-one");
    expect(text).toContain("超预算停止");
    // 关键：不能同时出现"完成"这个词，否则界面上分不出成败
    expect(text).not.toContain("完成");
  });

  it("等待审批时给出明显提示（父界面要能一眼看见子任务卡住了）", async () => {
    const host = await render(<ChildThreadCard t={child({ status: "waiting_approval" })} onOpen={() => undefined} />);
    expect(host.textContent).toContain("等待批准");
  });

  it("点「打开」回调会话 id", async () => {
    const got: string[] = [];
    const host = await render(<ChildThreadCard t={child()} onOpen={(id) => got.push(id)} />);
    const btn = [...host.querySelectorAll("button")].find((b) => (b.textContent ?? "").includes("打开"));
    expect(btn).toBeTruthy();
    await act(async () => {
      btn!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(got).toEqual(["20261028-120000-child1"]);
  });
});
