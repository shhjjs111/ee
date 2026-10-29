// Wave 0 反证（T02 / ACC-20、ACC-22）：钉住两处交互缺陷。
//
// 单独成文件而不是追加到 store.test.ts：那个文件所在的 store/lib 区域正在被
// 另一条工作线改动（UI 改版），共用同一文件会让归属与冲突都变模糊。
// 这里用 it.fails 表示「已知失败、待修」——现在不红，修好后会变成失败，
// 强制实施者回来把标记删掉，而不是把缺陷悄悄当成"已通过"。
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
    emitNotif: (m: string, p: any) => notifHs.forEach((h) => h(m, p)),
  };
}

const detail = (id: string, cwd: string) => ({ id, cwd, title: id, status: "idle", items: [], updated: 0 }) as any;

describe("Wave 0 反证：退回消息与停止反馈", () => {
  it.fails("两个非当前会话先后退回消息时，前一个不应被覆盖（ACC-20）", () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: "C", details: {}, returnedDraft: null });
    void useApp.getState().init(f.rpc);

    f.emitNotif("turn/queue_returned", { thread_id: "A", items: [{ id: "a1", text: "草稿 A" }] });
    f.emitNotif("turn/queue_returned", { thread_id: "B", items: [{ id: "b1", text: "草稿 B" }] });

    // 期望：A 的退回内容仍然可找回（按会话保存），而不是被 B 覆盖。
    const st = useApp.getState() as any;
    const all = st.returnedByThread ?? {};
    const hasA = JSON.stringify(all.A ?? st.returnedDraft ?? "").includes("草稿 A");
    expect(hasA).toBe(true);
  });

  it.fails("停止接口返回 interrupted:false 时必须有可见反馈（ACC-22）", async () => {
    const f = fakeRpc();
    useApp.setState({
      rpc: { ...f.rpc, call: async () => ({ interrupted: false }) } as any,
      activeThreadId: "T",
      details: { T: detail("T", "/A") },
      toast: null,
    });

    await useApp.getState().interrupt();
    await Promise.resolve();

    const st = useApp.getState();
    const surfaced = !!st.toast || (st as any).interruptRequests?.["T"] != null;
    expect(surfaced).toBe(true);
  });
});
