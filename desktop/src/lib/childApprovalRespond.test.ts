// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useApp } from "./store";

/** 父会话 p1 里有一个子会话 c1 卡在审批上。 */
function seed() {
  useApp.setState({
    activeThreadId: "p1",
    pendingChildApprovals: {
      p1: {
        parent_id: "p1",
        thread_id: "c1",
        kind: "approval",
        child_title: "子任务",
        call_id: "call_1",
        tool: "shell",
        summary: "rm -rf build",
      },
    },
  } as never);
}

describe("子会话审批：打到子线程，成功才清横幅", () => {
  beforeEach(() => {
    useApp.setState({
      rpc: null,
      pendingChildApprovals: {},
      activeThreadId: "p1",
    } as never);
  });

  it("RPC 指向子线程（不是当前会话）", async () => {
    const call = vi.fn().mockResolvedValue({});
    useApp.setState({ rpc: { call } } as never);
    seed();

    const ok = await useApp.getState().respondChild("p1", "c1", "call_1", "once");

    expect(ok).toBe(true);
    expect(call).toHaveBeenCalledWith(
      "approval/respond",
      expect.objectContaining({ thread_id: "c1", call_id: "call_1", choice: "once" }),
    );
    expect(useApp.getState().pendingChildApprovals.p1).toBeUndefined();
  });

  it("RPC 失败时保留横幅（否则点一下提示就消失，子会话还卡着）", async () => {
    const call = vi.fn().mockRejectedValue(new Error("这个审批已经处理过或已失效"));
    useApp.setState({ rpc: { call } } as never);
    seed();

    const ok = await useApp.getState().respondChild("p1", "c1", "call_1", "once");

    expect(ok).toBe(false);
    expect(useApp.getState().pendingChildApprovals.p1).toBeTruthy();
  });
});
