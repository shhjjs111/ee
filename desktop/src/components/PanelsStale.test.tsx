// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useApp } from "../lib/store";
import { RightPanel } from "./RightPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: { unmount(): void }[] = [];
afterEach(async () => {
  for (const r of roots.splice(0)) await act(async () => r.unmount());
  document.body.innerHTML = "";
});

async function mount(el: React.ReactElement) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(<TT.Provider>{el}</TT.Provider>));
}

const review = (branch: string) => ({ is_repo: true, git_available: true, branch, files: [], additions: 0, deletions: 0 });

describe("右栏改动审阅", () => {
  it("切换项目后，旧项目迟到的 git/diff 不覆盖新项目的改动列表", async () => {
    const pending: { cwd: string; resolve: (v: unknown) => void }[] = [];
    useApp.setState({
      rpc: { call: (_m: string, p: { cwd: string }) => new Promise((resolve) => pending.push({ cwd: p.cwd, resolve })) } as never,
      activeThreadId: null,
      activeWorkspace: "/A",
      rightOpen: true,
      rightTab: "review",
      view: "chat",
    });
    await mount(<RightPanel />);
    await act(async () => useApp.setState({ activeWorkspace: "/B" }));
    await act(async () => pending.find((p) => p.cwd === "/B")!.resolve(review("br-b")));
    await act(async () => pending.find((p) => p.cwd === "/A")!.resolve(review("br-a")));
    expect(document.body.textContent).toContain("br-b");
    expect(document.body.textContent).not.toContain("br-a");
  });

  it("同一项目连续刷新：先发的慢请求迟到，不覆盖后发的结果", async () => {
    const pending: ((v: unknown) => void)[] = [];
    useApp.setState({
      rpc: { call: () => new Promise((resolve) => pending.push(resolve)) } as never,
      activeThreadId: null,
      activeWorkspace: "/A",
      rightOpen: true,
      rightTab: "review",
      view: "chat",
      reviewNonce: 1,
    });
    await mount(<RightPanel />);
    await act(async () => useApp.setState({ reviewNonce: 2 }));
    expect(pending.length).toBe(2);
    await act(async () => pending[1](review("new")));
    await act(async () => pending[0](review("old")));
    expect(document.body.textContent).toContain("new");
    expect(document.body.textContent).not.toContain("old");
  });
});

describe("左栏操作失败不产生未处理的 promise", () => {
  it("addWorkspace / removeWorkspace / renameThread / deleteThread 失败时提示错误", async () => {
    const toast = vi.fn();
    useApp.setState({ rpc: { call: () => Promise.reject(new Error("目录不存在：X")) } as never, showToast: toast } as never);
    await expect(useApp.getState().addWorkspace("X")).resolves.toBe(false);
    await expect(useApp.getState().removeWorkspace("X")).resolves.toBe(false);
    await expect(useApp.getState().renameThread("t", "新名字")).resolves.toBe(false);
    await expect(useApp.getState().deleteThread("t")).resolves.toBe(false);
    expect(toast).toHaveBeenCalledTimes(4);
  });
});
