// @vitest-environment jsdom
// 侧栏会话列表的"显示更多"（Wave 2a）。
//
// 改版前是 g.threads.slice(0, 50)：第 51 个对话在侧栏里彻底不存在，也没有任何提示。
// 这里钉住三条：先显示 50、有多余时给出"再显示"、点开后能看到第 51 个。
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { ThreadSummary } from "../types";
import { Sidebar } from "./Sidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: { unmount(): void }[] = [];
afterEach(async () => {
  for (const r of roots.splice(0)) await act(async () => r.unmount());
  document.body.innerHTML = "";
});

const WORKSPACE = "C:\\proj\\demo";

function threads(n: number): ThreadSummary[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `t${i}`,
    title: `会话 ${String(i + 1).padStart(2, "0")}`,
    cwd: WORKSPACE,
    workspace: WORKSPACE,
    status: "idle",
    updated: 0,
  })) as unknown as ThreadSummary[];
}

async function mount(n: number) {
  useApp.setState({
    rpc: null,
    threads: threads(n),
    workspaces: [{ path: WORKSPACE, name: "demo" }] as never,
    activeThreadId: null,
    activeWorkspace: WORKSPACE,
    search: "",
    view: "chat",
    conn: "open",
  } as never);
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(<TT.Provider><Sidebar /></TT.Provider>));
}

const moreBtn = () => [...document.body.querySelectorAll("[data-testid='threads-more']")] as HTMLElement[];
const rowTitles = () =>
  [...document.body.querySelectorAll("[data-testid='thread-row']")].map((el) => el.getAttribute("data-title") ?? "");

describe("侧栏：会话超过 50 个", () => {
  it("默认只显示 50 条，并给出「再显示」入口", async () => {
    await mount(60);
    expect(rowTitles().length).toBe(50);
    expect(moreBtn().length).toBe(1);
    expect(moreBtn()[0].textContent).toContain("10");
  });

  it("点「再显示」后能看到第 51 条之后的会话", async () => {
    await mount(60);
    expect(rowTitles()).not.toContain("会话 60");
    await act(async () => moreBtn()[0].click());
    expect(rowTitles()).toContain("会话 60");
    expect(rowTitles().length).toBe(60);
    // 全部显示后不再有入口
    expect(moreBtn().length).toBe(0);
  });

  it("不超过 50 个时没有多余的入口", async () => {
    await mount(20);
    expect(rowTitles().length).toBe(20);
    expect(moreBtn().length).toBe(0);
  });
});
