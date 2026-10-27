// @vitest-environment jsdom
// 设置页各分区：切换项目后，旧项目迟到的响应不能覆盖新项目的内容；按钮触发的请求失败不能变成未处理的 promise。
import * as TT from "@radix-ui/react-tooltip";
import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useApp } from "../lib/store";
import type { Settings, SettingsResponse } from "../types";
import { AgentSettings } from "./AgentSettings";
import { AnswerSettings } from "./AnswerSettings";
import { McpSettings } from "./McpSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: { unmount(): void }[] = [];
afterEach(async () => {
  for (const r of roots.splice(0)) await act(async () => r.unmount());
  document.body.innerHTML = "";
});

type Pending = { method: string; params: any; resolve: (v: any) => void; reject: (e: Error) => void };

function deferredRpc(immediate: (method: string, params: any) => any) {
  const pending: Pending[] = [];
  const rpc = {
    call: (method: string, params: any = {}) => {
      const v = immediate(method, params);
      if (v !== undefined) return Promise.resolve(v);
      return new Promise((resolve, reject) => pending.push({ method, params, resolve, reject }));
    },
  };
  return { rpc: rpc as never, pending };
}

async function mount(el: React.ReactElement) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => root.render(<TT.Provider>{el}</TT.Provider>));
  roots.push(root);
  return root;
}

const mcpServer = (name: string) => ({
  name, transport: "stdio", command: "x", args: [], url: "", enabled: true, trusted: true, source: "project", allow_id: name,
  status: "idle", error: "", tools: [],
});

function McpHarness() {
  const [draft, setDraft] = useState<Settings>({
    mcp: { enabled: true, project_json: true, startup_timeout: 30, tool_timeout: 60, servers: {}, allowed_project: [] },
  } as unknown as Settings);
  return <McpSettings draft={draft} resp={{} as SettingsResponse} up={(p) => setDraft((d) => ({ ...d, ...p }))} />;
}

describe("设置页迟到响应", () => {
  it("MCP：切换项目后旧项目的 mcp/list 迟到，不覆盖新项目的服务器列表", async () => {
    const { rpc, pending } = deferredRpc(() => undefined);
    useApp.setState({ rpc, activeThreadId: null, activeWorkspace: "/A" });
    await mount(<McpHarness />);
    await act(async () => useApp.setState({ activeWorkspace: "/B" }));
    const a = pending.find((p) => p.method === "mcp/list" && p.params.cwd === "/A")!;
    const b = pending.find((p) => p.method === "mcp/list" && p.params.cwd === "/B")!;
    await act(async () => b.resolve({ enabled: true, servers: [mcpServer("srv-b")], errors: [], project_json: "/B/.mcp.json" }));
    await act(async () => a.resolve({ enabled: true, servers: [mcpServer("srv-a")], errors: [], project_json: "/A/.mcp.json" }));
    expect(document.body.textContent).toContain("srv-b");
    expect(document.body.textContent).not.toContain("srv-a");
  });

  it("记忆：切换项目后旧项目的 memory/get 迟到，不覆盖新项目的记忆内容", async () => {
    const { rpc, pending } = deferredRpc((method) => {
      if (method === "permissions/list") return { rules: [], path: "/h/p.json" };
      if (method === "skills/list") return { skills: [], errors: [], dirs: { user: "/h/skills", project: null } };
      return undefined;
    });
    useApp.setState({ rpc, activeThreadId: null, activeWorkspace: "/A" });
    const draft = { models: {}, providers: {}, memory: { enabled: true, workspace: true, max_bytes: 1000 } } as unknown as Settings;
    await mount(<AgentSettings draft={draft} resp={{ choices: {} } as unknown as SettingsResponse} up={() => undefined} />);
    await act(async () => useApp.setState({ activeWorkspace: "/B" }));
    const mem = (cwd: string, text: string) => ({ user: { path: "/h/m.md", exists: true, text: "u", entries: [] }, workspace: { path: `${cwd}/m.md`, exists: true, text, entries: [] } });
    const a = pending.find((p) => p.method === "memory/get" && p.params.cwd === "/A")!;
    const b = pending.find((p) => p.method === "memory/get" && p.params.cwd === "/B")!;
    await act(async () => b.resolve(mem("/B", "记忆-B")));
    await act(async () => a.resolve(mem("/A", "记忆-A")));
    const values = [...document.querySelectorAll("textarea")].map((t) => t.value).join("|");
    expect(values).toContain("记忆-B");
    expect(values).not.toContain("记忆-A");
  });

  it("回答与花费：点「刷新」的 budget/status 失败不产生未处理的 promise 拒绝", async () => {
    let calls = 0;
    const rpc = {
      call: (method: string) => {
        if (method === "reasoning/prefs") return Promise.resolve({ remembered: {} });
        if (method === "budget/status") {
          calls++;
          return calls === 1
            ? Promise.resolve({ currency: "¥", action: "warn", warn_ratio: 0.8, scopes: { session: { spent: 0, limit: 0 }, daily: { spent: 0, limit: 0 }, monthly: { spent: 0, limit: 0 } } })
            : Promise.reject(new Error("内核没有响应"));
        }
        return Promise.resolve({});
      },
    };
    const unhandled = vi.fn();
    const proc = (globalThis as any).process;
    proc.on("unhandledRejection", unhandled);
    useApp.setState({ rpc: rpc as never, activeThreadId: null, activeWorkspace: null });
    const draft = { models: {}, providers: {}, roles: {}, budget: {}, reasoning: {}, style: { default: "default" } } as unknown as Settings;
    const resp = { choices: {}, settings: draft } as unknown as SettingsResponse;
    await mount(<AnswerSettings draft={draft} resp={resp} up={() => undefined} />);
    const btn = [...document.querySelectorAll("button")].find((b) => b.textContent?.includes("刷新"));
    expect(btn).toBeTruthy();
    await act(async () => btn!.click());
    await new Promise((r) => setTimeout(r, 20));
    proc.off("unhandledRejection", unhandled);
    expect(unhandled).not.toHaveBeenCalled();
  });
});
