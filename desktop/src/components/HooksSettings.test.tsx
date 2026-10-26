// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { Settings, SettingsResponse } from "../types";
import { HooksSettings } from "./HooksSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

function Harness() {
  const [draft, setDraft] = useState<Settings>({
    hooks: { enabled: true, allowed_project: [], events: { PreToolUse: [{ matcher: "", command: "echo old", timeout: 60, shell: "", enabled: true, name: "" }] } },
  } as unknown as Settings);
  return (
    <TT.Provider>
      <HooksSettings draft={draft} resp={{} as SettingsResponse} up={(p) => setDraft((d) => ({ ...d, ...p }))} />
    </TT.Provider>
  );
}

function typeInto(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("HooksSettings 测试运行", () => {
  it("测试进行中改了命令，旧命令的结果不显示在新内容下面", async () => {
    let finishTest!: (v: unknown) => void;
    useApp.setState({
      activeThreadId: null,
      activeWorkspace: "/w",
      rpc: {
        call: (method: string) => {
          if (method === "hooks/list") return Promise.resolve({ enabled: true, user: [], projects: [], errors: [], events: [] });
          return new Promise((r) => (finishTest = r));
        },
      } as never,
    });
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => root.render(<Harness />));
    const btn = [...document.querySelectorAll("button")].find((b) => b.textContent?.includes("测试运行"))!;
    await act(async () => btn.click());
    const input = [...document.querySelectorAll("input")].find((i) => i.value === "echo old") as HTMLInputElement;
    await act(async () => typeInto(input, "echo new"));
    await act(async () =>
      finishTest({ label: "x", event: "PreToolUse", code: 0, stdout: "old-output", stderr: "", timed_out: false, error: "", duration_ms: 1, json: null, payload: {}, verdict: "继续", reason: "", context: "", permission: null, updated_input: null, notices: [] }),
    );
    expect(document.body.textContent).not.toContain("old-output");
    await act(async () => root.unmount());
  });
});
