// @vitest-environment jsdom
/**
 * 会话记录写盘失败要在界面上可见（后端 Thread.summary()["save_error"]）。
 *
 * 为什么需要组件层：后端用例（ee/tests/test_store_failure.py）只证明 summary 里
 * 有这个字段；字段到了前端、界面不渲染等于没有——磁盘满时用户照样以为对话存好了。
 * 两处消费点都守：对话上方的横幅（正在看的这个会话）与左栏行上的图标（别的会话）。
 */
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Conversation } from "./Conversation";
import { Sidebar } from "./Sidebar";
import { useApp } from "../lib/store";
import { useAutomations } from "../lib/automationStore";
import { useLoops } from "../lib/loopStore";
import type { ThreadDetail, ThreadSummary } from "../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let mounted: Root | null = null;

afterEach(() => {
  // 真卸载：残留组件仍订阅 store，下一个用例改 state 时会在 act 之外触发更新
  if (mounted) act(() => mounted!.unmount());
  mounted = null;
  document.body.innerHTML = "";
});

beforeEach(() => {
  useLoops.setState({ loops: [] } as never);
  useAutomations.setState({ automations: [] } as never);
});

function render(node: React.ReactNode) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  const root = createRoot(el);
  mounted = root;
  act(() => root.render(<TT.Provider>{node}</TT.Provider>));
  return el;
}

const SAVE_ERROR = { count: 3, error: "OSError: [Errno 28] No space left on device", at: 1 };

function thread(extra: Partial<ThreadSummary> = {}): ThreadSummary {
  return { id: "t1", title: "写盘失败", cwd: "/w", model: "m", mode: "default", status: "idle", ...extra };
}

function open(extra: Partial<ThreadDetail> = {}) {
  const t = thread(extra);
  act(() =>
    useApp.setState({
      rpc: null,
      activeThreadId: "t1",
      activeWorkspace: "/w",
      workspaces: ["/w"],
      rightOpen: false,
      threads: [t],
      details: { t1: { ...t, items: [], usage: {}, context_left: null, shell: null, git: null, ...extra } },
    } as never),
  );
  return useApp.getState().details.t1 as ThreadDetail;
}

describe("写盘失败在界面上可见", () => {
  it("当前会话保存不了：对话上方出现横幅，带原因与失败次数", () => {
    open({ save_error: SAVE_ERROR });
    const el = render(<Conversation />);
    const banner = el.querySelector("[data-testid='save-error-banner']");
    expect(banner, "磁盘写不进去时界面没有任何提示（用户会以为存好了）").toBeTruthy();
    const text = banner!.textContent || "";
    expect(text).toContain("没能保存到磁盘");
    expect(text).toContain("No space left on device"); // 原因要原样带上，否则用户不知道该清哪里
    expect(text).toContain("3"); // 失败次数
  });

  it("没有写盘失败时不显示横幅（不能常驻一条吓人的警告）", () => {
    open();
    const el = render(<Conversation />);
    expect(el.querySelector("[data-testid='save-error-banner']")).toBeNull();
  });

  it("左栏行上有标记：别的会话保存不了时，用户不进对话也能看到", async () => {
    const t = thread({ save_error: SAVE_ERROR });
    act(() => useApp.setState({ rpc: null, threads: [t], workspaces: [], search: "", view: "threads" } as never));
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    mounted = root;
    await act(async () => root.render(<TT.Provider><Sidebar /></TT.Provider>));
    expect(el.querySelector("[data-testid='thread-save-error']"), "左栏没有写盘失败的标记").toBeTruthy();
  });

  it("没失败的会话不显示该标记", async () => {
    act(() => useApp.setState({ rpc: null, threads: [thread()], workspaces: [], search: "", view: "threads" } as never));
    const el = document.createElement("div");
    document.body.appendChild(el);
    const root = createRoot(el);
    mounted = root;
    await act(async () => root.render(<TT.Provider><Sidebar /></TT.Provider>));
    expect(el.querySelector("[data-testid='thread-save-error']")).toBeNull();
  });
});
