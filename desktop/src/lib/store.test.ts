// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import type { ConnState, NotificationHandler, RpcClient } from "./rpc";
import { useApp } from "./store";

type Deferred = { method: string; params: any; resolve: (v: any) => void; reject: (e: Error) => void };

function fakeRpc() {
  const calls: Deferred[] = [];
  const stateHs = new Set<(s: ConnState) => void>();
  const notifHs = new Set<NotificationHandler>();
  const rpc = {
    state: "closed" as ConnState,
    call: (method: string, params: any = {}) =>
      new Promise((resolve, reject) => calls.push({ method, params, resolve, reject })),
    connect() {},
    close() {},
    onState: (h: (s: ConnState) => void) => (stateHs.add(h), () => stateHs.delete(h)),
    onNotification: (h: NotificationHandler) => (notifHs.add(h), () => notifHs.delete(h)),
  };
  return {
    rpc: rpc as unknown as RpcClient,
    calls,
    emitState: (s: ConnState) => stateHs.forEach((h) => h(s)),
    emitNotif: (m: string, p: any) => notifHs.forEach((h) => h(m, p)),
  };
}

const detail = (id: string, cwd: string) => ({ id, cwd, title: id, status: "idle", items: [], updated: 0 }) as any;

describe("store", () => {
  it("快速切换对话：先发的 thread/get 迟到返回，不会把当前项目改成旧对话的项目", async () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: null, activeWorkspace: "/A", details: {} });
    const pa = useApp.getState().openThread("a");
    const pb = useApp.getState().openThread("b");
    const ga = f.calls.find((c) => c.method === "thread/get" && c.params.thread_id === "a")!;
    const gb = f.calls.find((c) => c.method === "thread/get" && c.params.thread_id === "b")!;
    gb.resolve({ thread: detail("b", "/B") });
    await pb;
    ga.resolve({ thread: detail("a", "/A") });
    await pa;
    const st = useApp.getState();
    expect(st.activeThreadId).toBe("b");
    expect(st.activeWorkspace).toBe("/B");
    expect(st.details.a?.cwd).toBe("/A"); // 旧对话的详情照样缓存
  });

  it("内核重启换了新连接后，旧连接的状态变化不再改界面（不会误显示断线或再触发自动重启）", async () => {
    const old = fakeRpc();
    const fresh = fakeRpc();
    const s = useApp.getState();
    void s.init(old.rpc);
    void useApp.getState().init(fresh.rpc);
    fresh.emitState("open");
    expect(useApp.getState().conn).toBe("open");
    old.emitState("failed");
    expect(useApp.getState().conn).toBe("open");
  });

  it("旧连接的通知不再进 store", async () => {
    const old = fakeRpc();
    const fresh = fakeRpc();
    void useApp.getState().init(old.rpc);
    void useApp.getState().init(fresh.rpc);
    useApp.setState({ threads: [{ id: "t1", title: "x", status: "idle" } as any] });
    old.emitNotif("thread/deleted", { thread_id: "t1" });
    expect(useApp.getState().threads.map((t) => t.id)).toEqual(["t1"]);
    fresh.emitNotif("thread/deleted", { thread_id: "t1" });
    expect(useApp.getState().threads).toEqual([]);
  });
});

describe("发送失败", () => {
  it("空闲时 turn/start 失败：错误进对话，文字退回输入框（不丢用户打的字）", async () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: "t", details: { t: detail("t", "/A") }, returnedDraft: null, composerDraft: null });
    const p = useApp.getState().send("帮我改一下 README");
    await Promise.resolve();
    f.calls.find((c) => c.method === "turn/start")!.reject(new Error("内核 20 秒没有响应"));
    expect(await p).toBe(false);
    const st = useApp.getState();
    expect(st.details.t.status).toBe("idle");
    expect(st.details.t.items.some((x: any) => x.type === "error")).toBe(true);
    expect(st.returnedDraft?.texts).toEqual(["帮我改一下 README"]);
  });

  it("新对话 thread/start 失败：文字退回输入框", async () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: null, activeWorkspace: "/A", details: {}, composerDraft: null });
    const p = useApp.getState().send("你好");
    await Promise.resolve();
    f.calls.find((c) => c.method === "thread/start")!.reject(new Error("目录不存在"));
    expect(await p).toBe(false);
    expect(useApp.getState().composerDraft?.text).toBe("你好");
  });
});

describe("删除会话", () => {
  it("删掉之后详情、后台命令、计时缓存一起清掉（不留幽灵数据）", async () => {
    const f = fakeRpc();
    useApp.setState({
      rpc: f.rpc,
      activeThreadId: "t1",
      threads: [{ id: "t1", title: "a", status: "idle" } as any, { id: "t2", title: "b", status: "idle" } as any],
      details: { t1: detail("t1", "/A"), t2: detail("t2", "/B") },
      bgProcs: { t1: [{ id: 1 } as any] },
      turnStartedAt: { t1: 123 },
    });
    const p = useApp.getState().deleteThread("t1");
    await Promise.resolve();
    f.calls.find((c) => c.method === "thread/delete")!.resolve({ ok: true });
    expect(await p).toBe(true);
    const st = useApp.getState();
    expect(st.details.t1).toBeUndefined();
    expect(st.bgProcs.t1).toBeUndefined();
    expect(st.turnStartedAt.t1).toBeUndefined();
    expect(st.threads.map((x) => x.id)).toEqual(["t2"]);
    expect(st.activeThreadId).toBeNull();
  });

  it("别处删除（后端通知）也要清掉详情缓存", async () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: null, details: { t9: detail("t9", "/X") }, bgProcs: { t9: [{ id: 2 } as any] } });
    void useApp.getState().init(f.rpc);
    f.emitNotif("thread/deleted", { thread_id: "t9" });
    const st = useApp.getState();
    expect(st.details.t9).toBeUndefined();
    expect(st.bgProcs.t9).toBeUndefined();
  });
});
