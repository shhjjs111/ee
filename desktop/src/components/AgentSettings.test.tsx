// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { LspServerInfo, Settings, SettingsResponse } from "../types";
import { AgentSettings } from "./AgentSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

let latest: Settings | null = null;
/** `lsp/servers` 的返回值；每个用例自己设，mount() 里的假 rpc 直接读它。 */
let lspServers: LspServerInfo[] = [];

function Harness({ init }: { init: Partial<Settings> }) {
  const [draft, setDraft] = useState<Settings>({ models: {}, providers: {}, ...init } as unknown as Settings);
  latest = draft;
  const resp = { choices: {} } as unknown as SettingsResponse;
  return (
    <TT.Provider>
      <AgentSettings draft={draft} resp={resp} up={(p) => setDraft((d) => ({ ...d, ...p }))} />
    </TT.Provider>
  );
}

async function mount(init: Partial<Settings>) {
  useApp.setState({
    activeThreadId: null,
    activeWorkspace: null,
    rpc: {
      call: (method: string) => {
        if (method === "memory/get") return Promise.resolve({ user: { path: "/h/memory.md", exists: false, text: "", entries: [] }, workspace: null });
        if (method === "permissions/list") return Promise.resolve({ rules: [], path: "/h/permissions.json" });
        if (method === "skills/list") return Promise.resolve({ skills: [], errors: [], dirs: { user: "/h/skills", project: null } });
        if (method === "lsp/servers") return Promise.resolve({ servers: lspServers });
        return Promise.resolve({});
      },
    } as never,
  });
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => root.render(<Harness init={init} />));
  return { host, root };
}

/** 按行标签找数字输入框（Row 的结构：标签 div + 控件）。 */
function inputAfter(host: HTMLElement, label: string): HTMLInputElement {
  const row = [...host.querySelectorAll("div")].find((d) => d.textContent?.startsWith(label) && d.querySelector("input"));
  return row!.querySelector("input") as HTMLInputElement;
}

/** 受控输入框改值：直接赋 .value 不会触发 React 的 onChange，要走原型 setter + input 事件。 */
function typeInto(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function switchAfter(host: HTMLElement, label: string): HTMLButtonElement {
  const row = [...host.querySelectorAll("div")].find((d) => d.textContent?.startsWith(label) && d.querySelector("[role=switch]"));
  return row!.querySelector("[role=switch]") as HTMLButtonElement;
}

describe("工具与记忆设置：模式与开关", () => {
  it("默认完整模式；todo / present_plan / 记忆读入 / memory 工具开关写回草稿", async () => {
    const { host, root } = await mount({ memory: { enabled: true, workspace: true, max_bytes: 8192 } });
    const text = host.textContent ?? "";
    expect(text).toContain("工具模式");
    expect(text).toContain("完整（默认）");
    await act(async () => switchAfter(host, "任务清单（todo）").click());
    expect(latest?.todo).toBe(false);
    await act(async () => switchAfter(host, "计划提交（present_plan）").click());
    expect(latest?.present_plan).toBe(false);
    await act(async () => switchAfter(host, "memory 工具").click());
    expect(latest?.memory?.tool).toBe(false);
    expect(latest?.memory?.inject ?? true).toBe(true);
    await act(async () => switchAfter(host, "新对话读入记忆").click());
    expect(latest?.memory?.inject).toBe(false);
    await act(async () => root.unmount());
  });

  it("精简模式时提示工具开关不生效", async () => {
    const { host, root } = await mount({ profile: "lean" });
    expect(host.textContent).toContain("精简：下面的工具开关在精简模式下不生效");
    await act(async () => root.unmount());
  });

  it("命令沙箱：默认关、允许联网；开关写回草稿；不支持的系统给提示", async () => {
    const { host, root } = await mount({ sandbox: { enabled: false, network: true, supported: false } });
    expect(host.textContent).toContain("当前系统不支持（仅 Windows 生效）");
    await act(async () => switchAfter(host, "启用沙箱").click());
    expect(latest?.sandbox?.enabled).toBe(true);
    await act(async () => switchAfter(host, "允许联网").click());
    expect(latest?.sandbox?.network).toBe(false);
    expect(latest?.sandbox?.enabled).toBe(true);
    await act(async () => root.unmount());
  });
});

describe("语言服务器快照（lsp/servers）", () => {
  it("没有在跑的服务器时明确说「没在跑」，而不是空白", async () => {
    lspServers = [];
    const { host, root } = await mount({});
    expect(host.textContent).toContain("语言服务器");
    expect(host.textContent).toContain("现在没有在跑的语言服务器");
    await act(async () => root.unmount());
  });

  it("列出行：语言、会话标题、工作区根目录、引用数", async () => {
    lspServers = [{
      thread_id: "t1", title: "修复登录", root: "/w/repo", language: "python",
      identity: "pyright-langserver --stdio", refs: 0, alive: true,
    }];
    const { host, root } = await mount({});
    const text = host.textContent ?? "";
    expect(text).toContain("python");
    expect(text).toContain("修复登录");
    expect(text).toContain("/w/repo");
    expect(text).toContain("引用 0");
    await act(async () => root.unmount());
  });
});

describe("会话缓存设置（[session]）", () => {
  it("默认 40 个会话 / 60 秒，改数字写回草稿且不互相覆盖", async () => {
    const { host, root } = await mount({});
    const limit = inputAfter(host, "最多常驻会话数");
    const idle = inputAfter(host, "空闲多久才淘汰（秒）");
    expect(limit.value).toBe("40");
    expect(idle.value).toBe("60");

    await act(async () => typeInto(limit, "12"));
    expect(latest?.session?.max_loaded_threads).toBe(12);

    await act(async () => typeInto(idle, "120"));
    expect(latest?.session?.evict_idle_seconds).toBe(120);
    expect(latest?.session?.max_loaded_threads).toBe(12);
    await act(async () => root.unmount());
  });
});
