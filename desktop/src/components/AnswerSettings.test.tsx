// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { ApprovalItem, Settings, SettingsResponse } from "../types";
import { AnswerSettings } from "./AnswerSettings";
import { ApprovalCard } from "./Items";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

function typeInto(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

let latest: Settings | null = null;

function Harness() {
  const [draft, setDraft] = useState<Settings>({
    models: { "deepseek-flash": { provider: "deepseek", model: "deepseek-flash" }, "deepseek-pro": { provider: "deepseek", model: "deepseek-v4-pro" } },
    reasoning: { effort: "" },
    roles: { title: "", compact: "deepseek-pro", explore: "", plan: "", review: "", commit: "" },
    title: { ai: false },
    style: { default: "default" },
    budget: { session: 0, daily: 5, monthly: 0, action: "warn", warn_ratio: 0.8, currency: "¥" },
  } as unknown as Settings);
  latest = draft;
  const resp = {
    choices: { styles: [{ id: "default", label: "默认", description: "", source: "builtin", path: "" }, { id: "tutor", label: "作业辅导", description: "引导", source: "builtin", path: "" }] },
  } as unknown as SettingsResponse;
  return (
    <TT.Provider>
      <AnswerSettings draft={draft} resp={resp} up={(p) => setDraft((d) => ({ ...d, ...p }))} />
    </TT.Provider>
  );
}

describe("回答与花费设置（D5-9）", () => {
  it("显示用途模型、今日已花，改上限写回草稿", async () => {
    useApp.setState({
      activeThreadId: null,
      rpc: {
        call: (method: string) => {
          if (method === "reasoning/prefs") return Promise.resolve({ remembered: { "deepseek-flash": "high" } });
          if (method === "budget/status")
            return Promise.resolve({ currency: "¥", action: "warn", warn_ratio: 0.8, scopes: { session: { spent: 0, limit: 0 }, daily: { spent: 1.25, limit: 5 }, monthly: { spent: 3, limit: 0 } } });
          return Promise.resolve({});
        },
      } as never,
    });
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => root.render(<Harness />));
    const text = host.textContent ?? "";
    expect(text).toContain("上下文压缩");
    expect(text).toContain("deepseek-pro");
    expect(text).toContain("今天已花 ¥1.25");
    expect(text).toContain("deepseek-flash · 高");
    const inputs = [...host.querySelectorAll("input")] as HTMLInputElement[];
    await act(async () => typeInto(inputs[1], "8"));          // 每天
    expect(latest?.budget?.daily).toBe("8");
    await act(async () => root.unmount());
  });

  it("花费上限卡片：继续 / 本会话不再询问 / 停止", async () => {
    const calls: [string, string][] = [];
    useApp.setState({ respond: (async (cid: string, choice: string) => void calls.push([cid, choice])) as never });
    const item = { id: "a1", type: "approval", call_id: "budget_1", tool: "budget", title: "今日花费 ¥5.0000，上限 ¥5：已达到花费上限，是否继续？", detail: "", prefix: "", status: "pending", rule: "" } as unknown as ApprovalItem;
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () =>
      root.render(
        <TT.Provider>
          <ApprovalCard item={item} />
        </TT.Provider>,
      ),
    );
    expect(host.querySelector("[data-testid=budget-card]")).not.toBeNull();
    expect(host.textContent).not.toContain("本项目以后都允许");
    const btn = (label: string) => [...host.querySelectorAll("button")].find((b) => b.textContent?.includes(label))!;
    await act(async () => btn("继续这一回合").click());
    await act(async () => btn("本会话不再询问").click());
    await act(async () => btn("停止").click());
    expect(calls).toEqual([["budget_1", "once"], ["budget_1", "always"], ["budget_1", "deny"]]);
    await act(async () => root.unmount());
  });
});
