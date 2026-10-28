// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { onNotification, useApp } from "./store";
import type { ThreadSummary } from "../types";

function thread(over: Partial<ThreadSummary> & { id: string }): ThreadSummary {
  return {
    title: over.id,
    cwd: "C:/repo",
    model: "deepseek-flash",
    mode: "default",
    status: "idle",
    workspace: "C:/repo",
    ...over,
  };
}

/** 直接走通知入口：桌面端所有状态变化都从这里进来。 */
function notify(method: string, params: unknown) {
  onNotification(method, params);
}

describe("thread/child_approval（D6-1 U8）", () => {
  beforeEach(() => {
    useApp.setState({
      rpc: null,
      threads: [],
      details: {},
      activeThreadId: null,
      pendingChildApprovals: {},
    } as never);
  });

  it("子会话要审批时，父会话记住「哪个子会话卡住了」（不处理就会一直卡到超时）", () => {
    useApp.setState({
      threads: [thread({ id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child" })],
    } as never);

    notify("thread/child_approval", {
      parent_id: "p1",
      thread_id: "c1",
      kind: "approval",
      child_title: "子任务",
      call_id: "call_1",
      tool: "shell",
      summary: "rm -rf build",
    });

    const got = useApp.getState().pendingChildApprovals["p1"];
    expect(got).toBeTruthy();
    expect(got.thread_id).toBe("c1");
    expect(got.call_id).toBe("call_1");
    expect(got.tool).toBe("shell");
  });

  it("子会话的卡片状态显示为等待批准，用户不点进去也能看见", () => {
    useApp.setState({
      threads: [thread({ id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child" })],
    } as never);

    notify("thread/child_approval", {
      parent_id: "p1",
      thread_id: "c1",
      kind: "approval",
      child_title: "子任务",
      call_id: "call_1",
      tool: "shell",
      summary: "rm -rf build",
    });

    const t = useApp.getState().threads.find((x) => x.id === "c1");
    expect(t?.status).toBe("waiting_approval");
  });

  it("approval/respond 之后清掉待审批标记（否则卡片永远停在「等待」）", () => {
    useApp.setState({
      threads: [thread({ id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child" })],
    } as never);

    notify("thread/child_approval", {
      parent_id: "p1",
      thread_id: "c1",
      kind: "approval",
      child_title: "子任务",
      call_id: "call_1",
      tool: "shell",
      summary: "rm -rf build",
    });
    expect(useApp.getState().pendingChildApprovals["p1"]).toBeTruthy();

    notify("thread/updated", {
      thread: thread({ id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child", status: "running" }),
    });
    expect(useApp.getState().pendingChildApprovals["p1"]).toBeFalsy();
  });
});

describe("子会话改动数（U8 卡片的数据来源）", () => {
  beforeEach(() => {
    useApp.setState({
      rpc: null,
      threads: [],
      details: {},
      activeThreadId: null,
      pendingChildApprovals: {},
    } as never);
  });

  it("thread/updated 带来的 changed_count 要并进列表条目", () => {
    useApp.setState({
      threads: [thread({ id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child" })],
    } as never);

    notify("thread/updated", {
      thread: thread({
        id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child",
        changed_count: 3,
      }),
    });

    const t = useApp.getState().threads.find((x) => x.id === "c1");
    expect(t?.changed_count).toBe(3);
  });

  it("terminal_reason 也要能随 thread/updated 更新（卡片按它判终态）", () => {
    useApp.setState({
      threads: [thread({ id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child" })],
    } as never);

    notify("thread/updated", {
      thread: thread({
        id: "c1", title: "子任务", orchestrator_parent_id: "p1", orchestrator_role: "child",
        status: "idle",
        runs: [{ id: "r1", started: 1, status: "done", terminal_reason: "budget" }],
      }),
    });

    const t = useApp.getState().threads.find((x) => x.id === "c1");
    expect(t?.runs?.at(-1)?.terminal_reason).toBe("budget");
  });
});

describe("refreshChildStatus：主动补拉子会话状态", () => {
  it("按父会话逐个子会话查 thread/status，把 changed_count 写回列表", async () => {
    const calls: { method: string; params: unknown }[] = [];
    useApp.setState({
      rpc: {
        call: async (method: string, params: unknown) => {
          calls.push({ method, params });
          return { thread: { id: "c1", changed_count: 7, terminal_reason: "completed" } };
        },
      } as never,
      details: {},
      threads: [
        thread({ id: "p1", title: "父" }),
        thread({ id: "c1", title: "子", orchestrator_parent_id: "p1", orchestrator_role: "child" }),
      ],
    } as never);

    await useApp.getState().refreshChildStatus();

    expect(calls).toEqual([{ method: "thread/status", params: { parent_id: "p1", child_id: "c1" } }]);
    const t = useApp.getState().threads.find((x) => x.id === "c1");
    expect(t?.changed_count).toBe(7);
    expect(t?.terminal_reason).toBe("completed");
  });

  it("没有子会话时不发任何请求（别让普通会话也背这个开销）", async () => {
    let n = 0;
    useApp.setState({
      rpc: { call: async () => { n += 1; return {}; } } as never,
      details: {},
      threads: [thread({ id: "p1", title: "父" })],
    } as never);

    await useApp.getState().refreshChildStatus();
    expect(n).toBe(0);
  });

  it("单个子会话查失败不影响其他（父已删/子不在时静默跳过）", async () => {
    useApp.setState({
      rpc: {
        call: async (_m: string, p: { child_id: string }) => {
          if (p.child_id === "c_bad") throw new Error("父会话已删");
          return { thread: { id: "c_ok", changed_count: 2 } };
        },
      } as never,
      details: {},
      threads: [
        thread({ id: "p1", title: "父" }),
        thread({ id: "c_bad", title: "坏的", orchestrator_parent_id: "p1", orchestrator_role: "child" }),
        thread({ id: "c_ok", title: "好的", orchestrator_parent_id: "p1", orchestrator_role: "child" }),
      ],
    } as never);

    await useApp.getState().refreshChildStatus();
    const ok = useApp.getState().threads.find((x) => x.id === "c_ok");
    expect(ok?.changed_count).toBe(2);
  });
});
