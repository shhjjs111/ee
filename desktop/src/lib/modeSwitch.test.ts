// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useApp } from "./store";

describe("切换权限模式：打到会话，不随消息捎带", () => {
  beforeEach(() => {
    localStorage.clear();
    useApp.setState({
      rpc: null,
      activeThreadId: null,
      details: {},
      draftMode: "default",
    } as never);
  });

  it("有会话时调 thread/set_mode，并更新该会话的详情", async () => {
    const call = vi.fn().mockResolvedValue({ mode: "auto" });
    useApp.setState({ rpc: { call }, activeThreadId: "t1", details: { t1: { mode: "default" } } } as never);

    await useApp.getState().setMode("auto");

    expect(call).toHaveBeenCalledWith("thread/set_mode", { thread_id: "t1", mode: "auto" });
    expect(useApp.getState().details.t1?.mode).toBe("auto");
    expect(localStorage.getItem("ee.mode")).toBe("auto");   // 仍是新对话的初始值
  });

  it("没有会话时不打 RPC，只记成新对话的初始值", async () => {
    const call = vi.fn().mockResolvedValue({});
    useApp.setState({ rpc: { call } } as never);

    await useApp.getState().setMode("plan");

    expect(call).not.toHaveBeenCalled();
    expect(useApp.getState().draftMode).toBe("plan");
  });

  it("后端拒绝时提示错误，不改会话详情", async () => {
    const call = vi.fn().mockRejectedValue(new Error("子会话的权限模式由派发它的父会话决定"));
    useApp.setState({ rpc: { call }, activeThreadId: "c1", details: { c1: { mode: "read-only" } } } as never);

    await useApp.getState().setMode("auto");

    expect(useApp.getState().details.c1?.mode).toBe("read-only");
  });

  it("已有会话时发送消息不再携带 mode（否则发一句话就把会话提权）", async () => {
    const call = vi.fn().mockResolvedValue({ ok: true });
    useApp.setState({
      rpc: { call },
      activeThreadId: "t1",
      details: { t1: { status: "idle", mode: "read-only" } },
      draftMode: "auto",            // 全局草稿是 auto，但会话是只读
      threads: [],
    } as never);

    await useApp.getState().send("你好");

    const turnStart = call.mock.calls.find((c) => c[0] === "turn/start");
    expect(turnStart).toBeTruthy();
    expect(turnStart![1].mode).toBeUndefined();
  });
});
