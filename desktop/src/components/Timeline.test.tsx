// @vitest-environment jsdom
import * as Tooltip from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RpcClient } from "../lib/rpc";
import { useApp } from "../lib/store";
import { HunkDiff } from "./DiffView";
import { Timeline } from "./Timeline";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PATCH = ["diff --git a/a.txt b/a.txt", "--- a/a.txt", "+++ b/a.txt", "@@ -1,2 +1,2 @@", " x", "-old1", "+new1", "@@ -9,2 +9,2 @@", " y", "-old2", "+new2"].join("\n");

function mount(node: React.ReactNode) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(<Tooltip.Provider>{node}</Tooltip.Provider>));
  return host;
}

const click = (el: Element | null | undefined) => act(async () => (el as HTMLElement).click());
const byText = (host: HTMLElement, text: string) => [...host.querySelectorAll("button")].find((b) => b.textContent?.includes(text));

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("HunkDiff", () => {
  it("每块有接受/拒绝；已接受的块收起并可撤销", async () => {
    const onAction = vi.fn();
    const hunks = [
      { id: "h1", header: "@@ -1,2 +1,2 @@", additions: 1, deletions: 1, accepted: false },
      { id: "h2", header: "@@ -9,2 +9,2 @@", additions: 1, deletions: 1, accepted: true },
    ];
    const host = mount(<HunkDiff patch={PATCH} hunks={hunks} onAction={onAction} />);
    expect(host.textContent).toContain("new1");
    expect(host.textContent).not.toContain("new2"); // 已接受的块收起
    await click(byText(host, "拒绝"));
    expect(onAction).toHaveBeenCalledWith(hunks[0], "reject");
    await click(byText(host, "接受"));
    expect(onAction).toHaveBeenCalledWith(hunks[0], "accept");
    await click(byText(host, "撤销"));
    expect(onAction).toHaveBeenCalledWith(hunks[1], "unaccept");
  });
  it("块数对不上时退回普通 diff，不显示按钮", () => {
    const host = mount(<HunkDiff patch={PATCH} hunks={[{ id: "h1", header: "", additions: 1, deletions: 1, accepted: false }]} onAction={() => {}} />);
    expect(byText(host, "拒绝")).toBeUndefined();
    expect(host.textContent).toContain("new2");
  });
});

describe("Timeline", () => {
  afterEach(() => act(() => useApp.setState({ activeThreadId: null, details: {} })));

  it("新的在上；恢复前先算要改的文件、确认后再恢复", async () => {
    const calls: [string, Record<string, unknown>][] = [];
    const cps = [
      { id: "u1", kind: "turn", turn: 1, ts: 1, text: "第一条", before: "a", after: "b", undone: false, running: false, expired: false, after_expired: false, stat: { files: 1, additions: 2, deletions: 0 } },
      { id: "u2", kind: "turn", turn: 2, ts: 2, text: "第二条", before: "b", after: "c", undone: true, running: false, expired: false, after_expired: false, stat: { files: 0, additions: 0, deletions: 0 } },
    ];
    const rpc = {
      call: async (method: string, params: Record<string, unknown>) => {
        calls.push([method, params]);
        if (method === "checkpoint/list") return { available: true, checkpoints: cps };
        if (method === "checkpoint/diff") return { files: [{ path: "a.txt", additions: 1, deletions: 0, patch: PATCH, binary: false }], additions: 1, deletions: 0 };
        if (method === "checkpoint/restore") return { restored: ["a.txt"], label: "第 1 回合之前", restore_point: null };
        return {};
      },
    } as unknown as RpcClient;
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    let host!: HTMLElement;
    await act(async () => {
      useApp.setState({ rpc, activeThreadId: "t1", details: { t1: { id: "t1", status: "idle", cwd: "/w", items: [] } as never } });
      host = mount(<Timeline />);
    });
    const text = host.textContent ?? "";
    expect(text.indexOf("第 2 回合")).toBeLessThan(text.indexOf("第 1 回合"));
    expect(text).toContain("已撤销");
    expect(text).toContain("无改动");
    // 展开第 1 回合 → 看 diff → 恢复到之前
    const row = [...host.querySelectorAll("div.cursor-default")].find((d) => d.textContent?.includes("第一条"));
    await click(row);
    expect(calls.some(([m, p]) => m === "checkpoint/diff" && p.mode === "turn" && p.checkpoint_id === "u1")).toBe(true);
    await click(byText(host, "恢复到这回合之前"));
    expect(confirmSpy).toHaveBeenCalled();
    expect(confirmSpy.mock.calls[0][0]).toContain("a.txt");
    const r = calls.find(([m]) => m === "checkpoint/restore");
    expect(r?.[1]).toMatchObject({ thread_id: "t1", checkpoint_id: "u1", side: "before" });
  });

  it("取消确认就不恢复", async () => {
    const calls: string[] = [];
    const rpc = {
      call: async (method: string) => {
        calls.push(method);
        if (method === "checkpoint/list")
          return { available: true, checkpoints: [{ id: "rp", kind: "restore", ts: 3, text: "执行「恢复到第 1 回合之前」前的文件", before: "z", after: null, undone: false, running: false, expired: false, after_expired: false, stat: null }] };
        if (method === "checkpoint/diff") return { files: [{ path: "a.txt", additions: 1, deletions: 0, patch: "", binary: false }], additions: 1, deletions: 0 };
        return {};
      },
    } as unknown as RpcClient;
    vi.spyOn(window, "confirm").mockReturnValue(false);
    let host!: HTMLElement;
    await act(async () => {
      useApp.setState({ rpc, activeThreadId: "t1", details: { t1: { id: "t1", status: "idle", cwd: "/w", items: [] } as never } });
      host = mount(<Timeline />);
    });
    await click(host.querySelector("div.cursor-default"));
    await click(byText(host, "恢复到这个状态"));
    expect(calls).toContain("checkpoint/diff");
    expect(calls).not.toContain("checkpoint/restore");
  });
});
