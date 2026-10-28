// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { ThreadSummary, Workspace } from "../types";
import { Sidebar } from "./Sidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: { unmount(): void }[] = [];
afterEach(async () => {
  for (const r of roots.splice(0)) await act(async () => r.unmount());
  document.body.innerHTML = "";
});

const WS: Workspace = { path: "C:/repo", name: "repo" };

function thread(over: Partial<ThreadSummary> & { id: string }): ThreadSummary {
  return {
    title: over.id,
    cwd: "C:/repo",
    model: "deepseek-flash",
    mode: "default",
    status: "idle",
    workspace: "C:/repo",
    ...over,
  };
}

async function render(threads: ThreadSummary[]): Promise<HTMLElement> {
  useApp.setState({
    rpc: null,
    threads,
    workspaces: [WS],
    search: "",
    view: "chat",
    activeThreadId: null,
  } as never);
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(<TT.Provider><Sidebar /></TT.Provider>));
  return host;
}

/** 找到某标题所在的会话行。jsdom 不解析 Tailwind 的 CSS 规则（getComputedStyle 拿不到
 *  pl-7/pl-11 的像素值），所以断言 class 上的缩进档位——它正是样式来源。 */
function rowOf(host: HTMLElement, title: string): HTMLElement {
  const el = [...host.querySelectorAll<HTMLElement>("div")].find(
    (d) => d.className.includes("cursor-default") && (d.textContent ?? "").includes(title),
  );
  if (!el) throw new Error(`找不到会话行：${title}`);
  return el;
}

/** 缩进档：子任务用 pl-11，普通会话用 pl-7。 */
function indentOf(host: HTMLElement, title: string): string {
  const cls = rowOf(host, title).className;
  const m = cls.match(/\bpl-(\d+)\b/);
  return m ? m[1] : "";
}

describe("侧栏子会话与普通分叉的区分", () => {
  it("有 orchestrator_parent_id 的会话缩进显示（它属于父会话，是子任务）", async () => {
    const host = await render([
      thread({ id: "p1", title: "父会话" }),
      thread({ id: "c1", title: "子任务甲", orchestrator_parent_id: "p1", orchestrator_role: "child" }),
    ]);
    expect(indentOf(host, "父会话")).toBe("7");
    expect(indentOf(host, "子任务甲")).toBe("11");
  });

  it("普通分叉（只有 parent_id）不缩进——它不是被派发的子任务，不能混进树里", async () => {
    const host = await render([
      thread({ id: "p1", title: "父会话" }),
      thread({ id: "f1", title: "分叉对话", parent_id: "p1" }),
    ]);
    expect(indentOf(host, "分叉对话")).toBe("7");
  });

  it("子会话同时有 parent_id 与 orchestrator_parent_id 时按指挥关系缩进", async () => {
    const host = await render([
      thread({ id: "p1", title: "父会话" }),
      thread({
        id: "c1",
        title: "子任务乙",
        parent_id: "p1",
        orchestrator_parent_id: "p1",
        orchestrator_role: "child",
      }),
    ]);
    expect(indentOf(host, "子任务乙")).toBe("11");
  });
});

describe("子会话卡片接线（U8 的消费者）", () => {
  it("子会话在侧栏里渲染出卡片；普通会话没有", async () => {
    const host = await render([
      thread({ id: "p1", title: "父会话" }),
      thread({ id: "c1", title: "子任务甲", orchestrator_parent_id: "p1", orchestrator_role: "child" }),
      thread({ id: "n1", title: "普通会话" }),
    ]);
    const cards = host.querySelectorAll('[data-testid="child-thread-card"]');
    expect(cards.length).toBe(1);
    expect(cards[0].textContent).toContain("子任务甲");
  });
});
