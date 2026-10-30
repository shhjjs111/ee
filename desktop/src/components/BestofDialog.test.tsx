// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { RpcClient } from "../lib/rpc";
import { BestofDialog } from "./BestofDialog";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let roots: { unmount: () => void }[] = [];
afterEach(() => {
  act(() => {
    for (const r of roots) r.unmount();
  });
  roots = [];
  document.body.innerHTML = "";
});

function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => {
    root.render(<BestofDialog />);
  });
  return {
    view: () => document.body.textContent ?? "",
    buttons: () => [...document.body.querySelectorAll("button")] as HTMLButtonElement[],
  };
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

interface Call {
  method: string;
  params: Record<string, unknown>;
}

function fakeRpc(reply: (m: string, p: Record<string, unknown>) => unknown) {
  const calls: Call[] = [];
  const client = {
    call: async (method: string, params: Record<string, unknown>) => {
      calls.push({ method, params });
      return reply(method, params);
    },
  } as unknown as RpcClient;
  return { client, calls };
}

const wts = {
  repo: "/repo",
  worktrees: [
    { name: "a", path: "/wt/a", repo: "/repo", branch: "ee/a", base: "main", created: 0 },
    { name: "b", path: "/wt/b", repo: "/repo", branch: "ee/b", base: "main", created: 0 },
  ],
};

const picked = {
  winner: "b",
  candidates: [
    { id: "a", title: "候选 a", worktree: "/wt/a", run_id: "", cost: 0, verified: false, met: false, self_reported_ok: true, reason: "exit=1：命令以非零退出" },
    { id: "b", title: "候选 b", worktree: "/wt/b", run_id: "", cost: 0, verified: true, met: true, self_reported_ok: false, reason: "ok：3 个测试通过" },
  ],
  report: "best-of-n：2 个候选，胜者 b",
  cleanup: { keep: ["a", "b"], remove: [] },
};

describe("BestofDialog（候选比较）", () => {
  it("打开后拉 worktree 列表作为候选来源", async () => {
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const { client, calls } = fakeRpc((m) => (m === "worktree/list" ? wts : picked));
    act(() => useApp.setState({ rpc: client }));
    mount();
    await flush();
    expect(calls.some((c) => c.method === "worktree/list")).toBe(true);
    expect(document.body.textContent).toContain("ee/a");
    expect(document.body.textContent).toContain("ee/b");
  });

  it("比较后显示胜者与每个候选的证据", async () => {
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const { client } = fakeRpc((m) => (m === "worktree/list" ? wts : picked));
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();

    const compare = ui.buttons().find((b) => (b.textContent ?? "").includes("比较"));
    expect(compare, "没有「比较」按钮").toBeTruthy();
    act(() => compare!.click());
    await flush();

    const text = ui.view();
    expect(text).toContain("b");                       // 胜者
    expect(text).toContain("3 个测试通过");             // 胜者证据
    expect(text).toContain("命令以非零退出");           // 落选者证据
  });

  it("未验证的候选被标出来，不能混进胜者列", async () => {
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const { client } = fakeRpc((m) => (m === "worktree/list" ? wts : picked));
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();
    act(() => ui.buttons().find((b) => (b.textContent ?? "").includes("比较"))!.click());
    await flush();

    const rows = [...document.body.querySelectorAll("[data-verified]")];
    expect(rows.length).toBe(2);
    expect(rows.filter((r) => r.getAttribute("data-verified") === "true").length).toBe(1);
  });

  it("没有候选通过验证时明确说「没选」，不硬挑一个", async () => {
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const none = { ...picked, winner: null, candidates: picked.candidates.map((c) => ({ ...c, verified: false, met: false })) };
    const { client } = fakeRpc((m) => (m === "worktree/list" ? wts : none));
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();
    act(() => ui.buttons().find((b) => (b.textContent ?? "").includes("比较"))!.click());
    await flush();
    expect(ui.view()).toContain("没有候选通过验证");
  });

  it("worktree 少于 2 个时提示需要几个候选", async () => {
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const one = { repo: "/repo", worktrees: [wts.worktrees[0]] };
    const { client } = fakeRpc((m) => (m === "worktree/list" ? one : picked));
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();
    expect(ui.view()).toContain("至少两个");
  });
});

describe("BestofDialog（判据编排，bestof/verify）", () => {
  it("填了判据就走 bestof/verify，而不是只看不选的 bestof/pick", async () => {
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const { client, calls } = fakeRpc((m) => (m === "worktree/list" ? wts : picked));
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();

    const input = document.body.querySelector("input") as HTMLInputElement;
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "pytest -q");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await flush();

    act(() => ui.buttons().find((b) => (b.textContent ?? "").includes("验证"))!.click());
    await flush();

    const call = calls.find((c) => c.method === "bestof/verify");
    expect(call).toBeTruthy();
    expect((call!.params.goal as { command: string }).command).toBe("pytest -q");
    expect(calls.some((c) => c.method === "bestof/pick")).toBe(false);
  });

  it("留空判据时退回 bestof/pick，不假装能选出胜者", async () => {
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const { client, calls } = fakeRpc((m) => (m === "worktree/list" ? wts : picked));
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();

    act(() => ui.buttons().find((b) => (b.textContent ?? "").includes("比较"))!.click());
    await flush();

    expect(calls.some((c) => c.method === "bestof/pick")).toBe(true);
    expect(calls.some((c) => c.method === "bestof/verify")).toBe(false);
  });

  it("stale 候选显示「证据已过期」，且可以重新核对（ACC-16）", async () => {
    const staleRes = {
      winner: "b",
      candidates: [
        { id: "a", title: "候选 a", worktree: "/wt/a", run_id: "", cost: 0, verified: true, met: true,
          self_reported_ok: false, stale: true, verified_state: "s1", current_state: "s2",
          reason: "ok（验证后代码已变，证据过期）" },
        { id: "b", title: "候选 b", worktree: "/wt/b", run_id: "", cost: 0, verified: true, met: true,
          self_reported_ok: false, stale: false, verified_state: "s1", current_state: "s1", reason: "ok" },
      ],
      report: "x",
    };
    act(() => useApp.setState({ bestofDialog: "/repo" }));
    const { client, calls } = fakeRpc((m) => (m === "worktree/list" ? wts : staleRes));
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();

    const input = document.body.querySelector("input") as HTMLInputElement;
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "pytest -q");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await flush();
    act(() => ui.buttons().find((b) => (b.textContent ?? "").includes("验证"))!.click());
    await flush();

    expect(ui.view()).toContain("证据已过期");

    const re = ui.buttons().find((b) => (b.textContent ?? "").includes("重新核对"));
    expect(re).toBeTruthy();
    act(() => re!.click());
    await flush();
    expect(calls.some((c) => c.method === "bestof/recheck")).toBe(true);
  });
});
