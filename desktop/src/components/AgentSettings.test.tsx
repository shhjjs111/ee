// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { Settings, SettingsResponse } from "../types";
import { AgentSettings } from "./AgentSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

let latest: Settings | null = null;

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
