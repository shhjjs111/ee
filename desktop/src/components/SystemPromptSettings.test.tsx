// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import { differsFromFrozen, fileStatus, sourceLabel, type PromptSources, type SysPromptInfo } from "../lib/sysprompt";
import { SystemPromptSettings } from "./SystemPromptSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

const pending: PromptSources = {
  files: [
    { kind: "system", scope: "project", path: "/w/.ee/SYSTEM.md", allow_id: "aa:bb", trusted: false, active: false, error: "", chars: 10 },
    { kind: "append", scope: "user", path: "/h/APPEND_SYSTEM.md", allow_id: "", trusted: true, active: true, error: "", chars: 5 },
  ],
  model_patch: true,
  system: "",
  append: "/h/APPEND_SYSTEM.md",
  pending: ["aa:bb"],
};

describe("sysprompt 显示工具", () => {
  it("来源与状态文字", () => {
    expect(sourceLabel(pending, "system")).toBe("内置基础提示词");
    expect(sourceLabel(pending, "append")).toBe("用户：/h/APPEND_SYSTEM.md");
    expect(fileStatus(pending.files[0]).text).toContain("未信任");
    expect(fileStatus(pending.files[1]).text).toBe("生效中");
    expect(fileStatus({ ...pending.files[0], error: "文件是空的" }).tone).toBe("warn");
    const info: SysPromptInfo = { cwd: "/w", user_dir: "/h", current: pending, frozen: { ...pending, append: "" }, max_bytes: 65536 };
    expect(differsFromFrozen(info)).toBe(true);
    expect(differsFromFrozen({ ...info, frozen: pending })).toBe(false);
    expect(differsFromFrozen({ ...info, frozen: null })).toBe(false);
  });
});

describe("SystemPromptSettings", () => {
  it("显示未信任的项目文件，点信任后带 allow_id 调 RPC 并刷新；可查看最终系统提示词", async () => {
    const calls: [string, Record<string, unknown>][] = [];
    let trusted = false;
    useApp.setState({
      activeThreadId: null,
      activeWorkspace: "/w",
      rpc: {
        call: (method: string, params: Record<string, unknown>) => {
          calls.push([method, params]);
          if (method === "sysprompt/info") {
            const files = pending.files.map((f) => (f.scope === "project" && trusted ? { ...f, trusted: true, active: true } : f));
            return Promise.resolve({
              cwd: "/w",
              user_dir: "/h",
              current: { ...pending, files, system: trusted ? "/w/.ee/SYSTEM.md" : "", pending: trusted ? [] : ["aa:bb"] },
              frozen: null,
              max_bytes: 65536,
            });
          }
          if (method === "sysprompt/trust") {
            trusted = true;
            return Promise.resolve({ allowed_project: ["aa:bb"] });
          }
          if (method === "sysprompt/preview") return Promise.resolve({ text: "最终的系统提示词内容", frozen: false, model: "deepseek-flash", chars: 10 });
          return Promise.resolve({});
        },
      } as never,
    });
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () =>
      root.render(
        <TT.Provider>
          <SystemPromptSettings />
        </TT.Provider>,
      ),
    );
    expect(document.body.textContent).toContain("未信任（不生效）");
    expect(document.body.textContent).toContain("系统提示词不是安全边界");
    const trustBtn = [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === "信任")!;
    await act(async () => trustBtn.click());
    expect(calls.find(([m]) => m === "sysprompt/trust")?.[1]).toEqual({ cwd: "/w", allow_id: "aa:bb" });
    expect(document.body.textContent).toContain("项目：/w/.ee/SYSTEM.md");
    expect(document.body.textContent).toContain("撤销信任");
    const pv = [...document.querySelectorAll("button")].find((b) => b.textContent?.includes("查看最终系统提示词"))!;
    await act(async () => pv.click());
    expect(document.body.textContent).toContain("最终的系统提示词内容");
    expect(document.body.textContent).toContain("新对话会用的版本");
    await act(async () => root.unmount());
  });
});
