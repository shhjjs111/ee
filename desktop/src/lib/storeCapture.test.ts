// @vitest-environment jsdom
// 后端要截图（screenshot 工具）：前端要真的截一张并回送，失败也要回——不回就是白等 20 秒超时。
import { beforeEach, describe, expect, it } from "vitest";
import { onNotification, useApp } from "./store";

type Call = { method: string; params: Record<string, unknown> };

function setup(capture?: () => Promise<{ data_url?: string; error?: string }>) {
  const calls: Call[] = [];
  useApp.setState({
    rpc: {
      call: (method: string, params: Record<string, unknown>) => {
        calls.push({ method, params });
        return Promise.resolve({ ok: true });
      },
    } as never,
  } as never);
  (window as unknown as { ee: Record<string, unknown> }).ee = { captureWindow: capture ?? (() => Promise.resolve({ data_url: "data:image/png;base64,AAA" })) };
  return calls;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("ui/capture", () => {
  beforeEach(() => {
    useApp.setState({ rpc: null } as never);
  });

  it("截到图就回送 data_url", async () => {
    const calls = setup();
    onNotification("ui/capture", { thread_id: "t1", id: "shot_1" });
    await flush();
    expect(calls).toEqual([
      {
        method: "ui/capture_result",
        params: { thread_id: "t1", id: "shot_1", data_url: "data:image/png;base64,AAA", error: "" },
      },
    ]);
  });

  it("窗口截不了时把原因送回（不能静默，后端会一直等到超时）", async () => {
    const calls = setup(() => Promise.resolve({ error: "窗口不存在（可能已关到托盘）" }));
    onNotification("ui/capture", { thread_id: "t1", id: "shot_2" });
    await flush();
    expect(calls[0].params).toMatchObject({ id: "shot_2", data_url: "", error: "窗口不存在（可能已关到托盘）" });
  });

  it("截图抛错也要回送", async () => {
    const calls = setup(() => Promise.reject(new Error("截图崩了")));
    onNotification("ui/capture", { thread_id: "t1", id: "shot_3" });
    await flush();
    expect(calls[0].params).toMatchObject({ id: "shot_3", error: "截图崩了" });
  });

  it("没有桥（浏览器里跑前端）时回送「不支持」，不抛异常", async () => {
    const calls = setup(() => Promise.reject(new Error("no bridge")));
    (window as unknown as { ee: Record<string, unknown> }).ee = {};
    onNotification("ui/capture", { thread_id: "t1", id: "shot_4" });
    await flush();
    expect(calls[0].params).toMatchObject({ id: "shot_4", error: "当前环境不支持截图" });
  });

  it("RPC 报错（后端已重连）时不往外抛", async () => {
    useApp.setState({
      rpc: {
        call: () => Promise.reject(new Error("与后端的连接断开了")),
      } as never,
    } as never);
    (window as unknown as { ee: Record<string, unknown> }).ee = { captureWindow: () => Promise.resolve({ data_url: "data:image/png;base64,AAA" }) };
    expect(() => onNotification("ui/capture", { thread_id: "t1", id: "shot_5" })).not.toThrow();
    await flush();
  });
});
