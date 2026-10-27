import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { errMsg, installGlobalErrorHandlers, reportError } from "./errors";
import { useApp } from "./store";
import { useAutomations } from "./automationStore";

const showToast = vi.fn();
beforeEach(() => {
  showToast.mockReset();
  useApp.setState({ showToast } as never);
});
afterEach(() => vi.restoreAllMocks());

describe("失败提示", () => {
  it("reportError 带前缀 toast", () => {
    reportError("停止失败")(new Error("连接断开"));
    expect(showToast).toHaveBeenCalledWith("停止失败：连接断开", "error");
    expect(errMsg("x")).toBe("x");
  });

  it("未处理的 Promise 拒绝会弹提示", () => {
    const off = installGlobalErrorHandlers();
    const ev = new Event("unhandledrejection") as PromiseRejectionEvent;
    Object.assign(ev, { reason: new Error("内核没有响应") });
    window.dispatchEvent(ev);
    off();
    expect(showToast).toHaveBeenCalledWith("操作失败：内核没有响应", "error");
  });

  it("停止生成失败时提示，而不是静默", async () => {
    useApp.setState({ activeThreadId: "t1", rpc: { call: () => Promise.reject(new Error("超时")) } as never });
    await useApp.getState().interrupt();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining("超时"), "error");
  });

  it("连接后加载对话/项目列表失败时提示", async () => {
    let onState: (s: string) => void = () => undefined;
    const rpc = {
      call: (m: string) => (m === "workspace/list" ? Promise.reject(new Error("内核异常")) : Promise.resolve({ threads: [], models: [], settings: null })),
      onState: (cb: (s: string) => void) => ((onState = cb), () => undefined),
      onNotification: () => () => undefined,
      connect: () => undefined,
    };
    useApp.setState({ rpc: rpc as never });
    await useApp.getState().refreshListsSafe();
    expect(showToast).toHaveBeenCalledWith(expect.stringMatching(/加载.*失败：内核异常/), "error");
    void onState;
  });

  it("自动化列表加载失败时提示", async () => {
    useApp.setState({ rpc: { call: () => Promise.reject(new Error("坏了")) } as never });
    await useAutomations.getState().refreshSafe();
    expect(showToast).toHaveBeenCalledWith(expect.stringMatching(/自动化.*失败：坏了/), "error");
  });
});
