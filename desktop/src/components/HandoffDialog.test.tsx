// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { RpcClient } from "../lib/rpc";
import { HandoffDialog } from "./HandoffDialog";

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
    root.render(<HandoffDialog />);
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

const manifest = {
  root: "/repo",
  ignored_policy: "skip",
  entries: [
    { path: "a.py", category: "unstaged", kind: "file", size: 10, sha256: "a".repeat(64), binary: false, selected: true },
    { path: "debug.log", category: "ignored", kind: "file", size: 4, sha256: "b".repeat(64), binary: false, selected: false },
  ],
  summary: { total: 2, selected: 1, bytes: 10, binary: 0, by_category: { unstaged: 1, ignored: 1 } },
};

describe("HandoffDialog（文件交接清单）", () => {
  it("打开后自动拉清单，显示条目与摘要", async () => {
    act(() => useApp.setState({ handoffDialog: "/repo" }));
    const { client, calls } = fakeRpc(() => manifest);
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();

    expect(calls.some((c) => c.method === "handoff/manifest")).toBe(true);
    const text = ui.view();
    expect(text).toContain("a.py");
    expect(text).toContain("debug.log");       // 忽略文件也必须列出来
    expect(text).toContain("迁移清单");
  });

  it("忽略文件标记为不迁移，且能被看见", async () => {
    act(() => useApp.setState({ handoffDialog: "/repo" }));
    const { client } = fakeRpc(() => manifest);
    act(() => useApp.setState({ rpc: client }));
    mount();
    await flush();

    const rows = [...document.body.querySelectorAll("[data-category]")];
    expect(rows.length).toBe(2);
    const ignored = rows.find((r) => r.getAttribute("data-category") === "ignored");
    expect(ignored?.getAttribute("data-selected")).toBe("false");
  });

  it("每条显示短哈希，证明可核对", async () => {
    act(() => useApp.setState({ handoffDialog: "/repo" }));
    const { client } = fakeRpc(() => manifest);
    act(() => useApp.setState({ rpc: client }));
    mount();
    await flush();
    expect(document.body.textContent).toContain("aaaaaaaaaaaa"); // sha256 前 12 位
  });

  it("服务端报错时显示，而不是空清单冒充没东西", async () => {
    act(() => useApp.setState({ handoffDialog: "/repo" }));
    const { client } = fakeRpc(() => {
      throw new Error("生成清单失败（不是 git 仓库？）");
    });
    act(() => useApp.setState({ rpc: client }));
    const ui = mount();
    await flush();
    expect(ui.view()).toContain("不是 git 仓库");
  });

  it("没有目标路径时不做核对（不猜一个目录）", async () => {
    act(() => useApp.setState({ handoffDialog: "/repo" }));
    const { client, calls } = fakeRpc((m) => (m === "handoff/manifest" ? manifest : { ok: true, problems: [] }));
    act(() => useApp.setState({ rpc: client }));
    mount();
    await flush();
    expect(calls.some((c) => c.method === "handoff/verify")).toBe(false);
  });
});
