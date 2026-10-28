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
