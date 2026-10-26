// 自动化编辑弹窗：调度预览的乱序响应、每天的时间输入不合法时的提示与保存拦截。
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAutomations } from "../lib/automationStore";
import { defaultAutomation } from "../lib/schedule";
import { useApp } from "../lib/store";
import { AutomationsPage } from "./AutomationsPage";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Pending = { method: string; params: any; resolve: (v: any) => void; reject: (e: any) => void };

let root: Root;
let host: HTMLDivElement;
let calls: Pending[];

function setInput(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

const previewText = () => document.body.querySelector(".mt-1\\.5.text-\\[11\\.5px\\]")?.textContent ?? "";

beforeEach(() => {
  vi.useFakeTimers();
  calls = [];
  const rpc = {
    call: (method: string, params: any) => new Promise((resolve, reject) => calls.push({ method, params, resolve, reject })),
    onNotification: () => () => undefined,
    onState: () => () => undefined,
  };
  useApp.setState({ rpc: rpc as any, workspaces: [], models: [] } as any);
  useAutomations.setState({ automations: [], runs: [], unread: 0, loaded: true, editing: null });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root.render(<AutomationsPage />));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
});

describe("AutomationsPage 编辑弹窗", () => {
  it("预览只采用最新一次请求的结果（旧请求晚到不覆盖）", async () => {
    act(() => useAutomations.setState({ editing: { ...defaultAutomation("/p"), schedule: { kind: "cron", expr: "0 9 * * *" } } }));
    act(() => vi.advanceTimersByTime(300));
    const input = document.body.querySelector("input.mono") as HTMLInputElement;
    act(() => setInput(input, "bad"));
    act(() => vi.advanceTimersByTime(300));
    const previews = calls.filter((c) => c.method === "automations/preview");
    expect(previews.length).toBe(2);
    await act(async () => previews[1].resolve({ ok: false, error: "cron 表达式要正好 5 段", next_runs: [] }));
    await act(async () => previews[0].resolve({ ok: true, error: "", next_runs: [1800000000] }));
    expect(previewText()).toContain("cron 表达式要正好 5 段");
  });

  it("关掉弹窗再打开时不显示上一次的预览", async () => {
    act(() => useAutomations.setState({ editing: { ...defaultAutomation("/p"), schedule: { kind: "cron", expr: "0 0 30 2 *" } } }));
    act(() => vi.advanceTimersByTime(300));
    await act(async () => calls[0].resolve({ ok: false, error: "不会触发", next_runs: [] }));
    expect(previewText()).toContain("不会触发");
    act(() => useAutomations.setState({ editing: null }));
    act(() => useAutomations.setState({ editing: defaultAutomation("/p") }));
    expect(previewText()).not.toContain("不会触发");
  });

  it("每天的时间写错时提示，并且不能保存", async () => {
    const save = vi.fn(async () => true);
    useAutomations.setState({ save } as any);
    act(() => useAutomations.setState({ editing: { ...defaultAutomation("/p"), name: "x", prompt: "y" } }));
    const time = Array.from(document.body.querySelectorAll("input")).find((i) => i.value === "09:00") as HTMLInputElement;
    act(() => setInput(time, "25:00"));
    expect(document.body.textContent).toContain("HH:MM");
    const btn = Array.from(document.body.querySelectorAll("button")).find((b) => b.textContent?.includes("创建")) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    act(() => setInput(time, "10:30"));
    expect(btn.disabled).toBe(false);
    await act(async () => btn.click());
    expect(save).toHaveBeenCalledTimes(1);
    expect((save.mock.calls[0] as any[])[0].schedule.time).toBe("10:30");
  });
});
