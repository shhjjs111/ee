import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RpcClient } from "./rpc";

class FakeWS {
  static OPEN = 1;
  static all: FakeWS[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  constructor(public url: string) {
    FakeWS.all.push(this);
  }
  send(m: string) {
    this.sent.push(m);
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

beforeEach(() => {
  FakeWS.all = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeWS as any);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("RpcClient", () => {
  it("连接前排队的请求超时后不再在连上时补发（避免界面报失败、后端却执行了）", async () => {
    const c = new RpcClient("ws://x");
    c.connect();
    const p = c.call("turn/start", { text: "hi" }, 1000).catch((e) => e);
    vi.advanceTimersByTime(1100);
    expect(String(await p)).toMatch(/没有响应/);
    FakeWS.all[0].open();
    expect(FakeWS.all[0].sent).toEqual([]);
  });

  it("断线时排队中的请求被拒绝后，重连后也不补发", async () => {
    const c = new RpcClient("ws://x");
    c.connect();
    FakeWS.all[0].open();
    FakeWS.all[0].close(); // 断线
    const p = c.call("turn/start", {}).catch((e) => e);
    // 第二次断线：排队中的请求被拒绝
    vi.advanceTimersByTime(400);
    FakeWS.all[1].close();
    expect(String(await p)).toMatch(/断开/);
    vi.advanceTimersByTime(1000);
    FakeWS.all[2].open();
    expect(FakeWS.all[2].sent).toEqual([]);
  });

  it("close() 之后不会再被退避定时器重新连上", () => {
    const c = new RpcClient("ws://x");
    const states: string[] = [];
    c.onState((s) => states.push(s));
    c.connect();
    FakeWS.all[0].open();
    FakeWS.all[0].close(); // 意外断线，安排了重连
    c.close();
    vi.advanceTimersByTime(30000);
    expect(FakeWS.all.length).toBe(1);
    expect(states).not.toContain("failed");
  });

  it("旧连接迟到的 close 事件不影响新连接上的请求", async () => {
    const c = new RpcClient("ws://x");
    c.connect();
    const old = FakeWS.all[0];
    c.connect(); // 例如重连期间又显式连了一次
    const fresh = FakeWS.all[1];
    fresh.open();
    let settled = false;
    const p = c.call("app/info").then((r) => {
      settled = true;
      return r;
    });
    old.close();
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(c.state).toBe("open");
    const id = JSON.parse(fresh.sent[0]).id;
    fresh.onmessage?.({ data: JSON.stringify({ id, result: { ok: 1 } }) });
    expect(await p).toEqual({ ok: 1 });
  });
});
