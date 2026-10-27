// 自动化编辑弹窗：调度预览的乱序响应、每天的时间输入不合法时的提示与保存拦截。
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAutomations } from "../lib/automationStore";
import { defaultAutomation } from "../lib/schedule";
import { useApp } from "../lib/store";
import * as Tooltip from "@radix-ui/react-tooltip";
import { AutomationsPage } from "./AutomationsPage";

const realSave = useAutomations.getState().save;

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
  useAutomations.setState({ automations: [], runs: [], unread: 0, loaded: true, editing: null, filter: "", save: realSave });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root.render(<Tooltip.Provider><AutomationsPage /></Tooltip.Provider>));
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

const RUN = {
  id: "ru_1",
  automation_id: "au_1",
  name: "夜间修复",
  trigger: "manual" as const,
  started: 1800000000,
  finished: 1800000200,
  status: "completed",
  thread_id: "t1",
  summary: "改好了",
  unread: false,
  duration_ms: 200000,
  usage: { prompt: 100, completion: 10, cost: 0.0123, currency: "¥" },
  log: "改好了，详细说明：" + "x".repeat(50),
  worktree: {
    path: "/wt/auto-夜间修复-20270115-0800",
    branch: "ee/auto-夜间修复-20270115-0800",
    state: "pending" as const,
    files: [{ path: "src/a.ts", additions: 3, deletions: 1 }],
    file_count: 1,
    additions: 3,
    deletions: 1,
  },
};

describe("AutomationsPage 运行历史（D5-3）", () => {
  it("显示用时、花费、worktree 改动，并能合并 / 丢弃、展开日志", async () => {
    act(() => useAutomations.setState({ runs: [RUN] }));
    const row = document.body.querySelector("[data-testid=run-row]")!;
    expect(row.textContent).toContain("用时 3 分 20 秒");
    expect(row.textContent).toContain("¥0.0123");
    expect(row.textContent).toContain("1 个文件 +3 −1");
    expect(row.textContent).toContain("src/a.ts");
    const btn = (label: string) => Array.from(row.querySelectorAll("button")).find((b) => b.textContent?.includes(label)) as HTMLButtonElement;
    act(() => btn("查看完整日志").click());
    expect(row.querySelector("pre")?.textContent).toContain("详细说明");
    act(() => btn("合并").click());
    const c = calls.find((x) => x.method === "automations/worktree")!;
    expect(c.params).toEqual({ run_id: "ru_1", action: "merge" });
    expect(calls.some((x) => x.method === "thread/get")).toBe(false); // 点按钮不打开会话
    await act(async () => c.resolve({ run: { ...RUN, worktree: { ...RUN.worktree, state: "merged" } } }));
    expect(document.body.querySelector("[data-testid=run-row]")!.textContent).toContain("已合并");
    expect(Array.from(document.body.querySelectorAll("button")).some((b) => b.textContent?.includes("丢弃"))).toBe(false);
  });

  it("运行中的 worktree 不显示合并/丢弃；可以按自动化筛选", () => {
    act(() =>
      useAutomations.setState({
        runs: [
          { ...RUN, status: "running", finished: null, worktree: { ...RUN.worktree, state: "running" } },
          { ...RUN, id: "ru_2", automation_id: "au_2", name: "别的", worktree: null },
        ],
      }),
    );
    expect(document.body.querySelectorAll("[data-testid=run-row]").length).toBe(2);
    expect(Array.from(document.body.querySelectorAll("button")).some((b) => b.textContent?.includes("丢弃"))).toBe(false);
    act(() => useAutomations.setState({ filter: "au_2" }));
    const rows = document.body.querySelectorAll("[data-testid=run-row]");
    expect(rows.length).toBe(1);
    expect(rows[0].textContent).toContain("别的");
  });

  it("由 /loop 转来的自动化保存后停掉原循环", async () => {
    useAutomations.setState({ filter: "" });
    const p = useAutomations.getState().save({ ...defaultAutomation("/p"), name: "x", prompt: "y", fromLoop: { id: "2", threadId: "t9" } });
    await act(async () => calls.find((x) => x.method === "automations/create")!.resolve({ automation: { id: "au_9", ...defaultAutomation("/p") } }));
    const create = calls.find((x) => x.method === "automations/create")!;
    expect(create.params.automation.fromLoop).toBeUndefined();
    await act(async () => calls.find((x) => x.method === "loop/stop")!.resolve({ stopped: ["2"] }));
    expect(calls.find((x) => x.method === "loop/stop")!.params).toEqual({ thread_id: "t9", id: "2" });
    expect(await p).toBe(true);
  });
});
