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
