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
  it("两个非当前会话先后退回消息时，前一个不应被覆盖（ACC-20）", () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: "C", details: {}, returnedDraft: null, returnedInbox: {} });
    void useApp.getState().init(f.rpc);

    f.emitNotif("turn/queue_returned", { thread_id: "A", items: [{ id: "a1", text: "草稿 A" }] });
    f.emitNotif("turn/queue_returned", { thread_id: "B", items: [{ id: "b1", text: "草稿 B" }] });

    // 按会话保存：两个会话各自的退回内容都必须还在。
    const box = useApp.getState().returnedInbox;
    expect(box.A?.map((x) => x.text)).toEqual(["草稿 A"]);
    expect(box.B?.map((x) => x.text)).toEqual(["草稿 B"]);
    // 当前会话是 C，不该被别人的退回内容打断
    expect(useApp.getState().returnedDraft).toBeNull();
  });

  it("当前会话收到退回时才派生给输入框（ACC-20 补充）", () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: "A", details: {}, returnedDraft: null, returnedInbox: {} });
    void useApp.getState().init(f.rpc);

    f.emitNotif("turn/queue_returned", { thread_id: "A", items: [{ id: "a1", text: "草稿 A" }] });
    expect(useApp.getState().returnedDraft?.texts).toEqual(["草稿 A"]);
  });

  it("停止接口返回 interrupted:false 时必须有可见反馈（ACC-22）", async () => {
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

describe("Wave 3 / T13：从后端 runs 恢复真实计时", () => {
  it("runs 里的秒要换成毫秒（直接减 Date.now 会差三个数量级）", async () => {
    const { latestRunStartedMs } = await import("./store");
    const startedSec = 1_700_000_000;                 // 后端给的是 epoch 秒
    const ms = latestRunStartedMs({ runs: [{ id: "r1", status: "running", started: startedSec }] } as any);
    expect(ms).toBe(startedSec * 1000);
  });

  it("只认还在 running 的那条", async () => {
    const { latestRunStartedMs } = await import("./store");
    const ms = latestRunStartedMs({
      runs: [
        { id: "r1", status: "running", started: 1_700_000_000 },
        { id: "r2", status: "done", started: 1_700_000_100 },
      ],
    } as any);
    expect(ms).toBe(1_700_000_000_000);
  });

  it("没有运行中的 run 就不给起点（界面应不显示秒数，而不是造一个假起点）", async () => {
    const { latestRunStartedMs } = await import("./store");
    expect(latestRunStartedMs({ runs: [{ id: "r1", status: "done", started: 1 }] } as any)).toBeUndefined();
    expect(latestRunStartedMs({ runs: [] } as any)).toBeUndefined();
    expect(latestRunStartedMs(undefined)).toBeUndefined();
  });

  it("切回运行中的会话会补写计时起点", async () => {
    const f = fakeRpc();
    useApp.setState({ rpc: f.rpc, activeThreadId: null, details: {}, turnStartedAt: {} });
    const p = useApp.getState().openThread("T9");
    const started = 1_700_000_000;
    f.calls.find((c) => c.method === "thread/get")!.resolve({
      thread: { ...detail("T9", "/A"), status: "running", runs: [{ id: "r", status: "running", started }] },
    });
    await p;
    expect(useApp.getState().turnStartedAt.T9).toBe(started * 1000);
  });
});
