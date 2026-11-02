// @vitest-environment jsdom
// 更新卡片：状态映射与按钮行为。不测 electron-updater 本身（在 updater.test.ts 里注入假的）。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UpdateState } from "../types";
import { UpdateCard } from "./UpdateCard";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
  delete (window as unknown as { ee?: unknown }).ee;
});

type Bridge = {
  state: ReturnType<typeof vi.fn>;
  check: ReturnType<typeof vi.fn>;
  install: ReturnType<typeof vi.fn>;
  openRelease: ReturnType<typeof vi.fn>;
  onState: (cb: (s: UpdateState) => void) => () => void;
  push: (s: UpdateState) => void;
};

function bridge(initial: UpdateState): Bridge {
  const listeners: Array<(s: UpdateState) => void> = [];
  return {
    state: vi.fn(async () => initial),
    check: vi.fn(async () => ({ ...initial, phase: "up-to-date" as const, message: "已是最新版本" })),
    install: vi.fn(async () => true),
    openRelease: vi.fn(async () => undefined),
    onState: (cb) => {
      listeners.push(cb);
      return () => {
        const i = listeners.indexOf(cb);
        if (i >= 0) listeners.splice(i, 1);
      };
    },
    push: (s) => listeners.forEach((f) => f(s)),
  };
}

const base: UpdateState = { phase: "idle", current: "0.1.0" };

/** 挂载并等第一帧快照落地（按钮/文案都依赖它）。 */
async function mount(st: UpdateState = base): Promise<{ host: HTMLElement; b: Bridge }> {
  const b = bridge(st);
  (window as unknown as { ee?: unknown }).ee = { update: b };
  const host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    createRoot(host).render(<UpdateCard />);
  });
  return { host, b };
}

const text = (host: HTMLElement) => host.textContent ?? "";
const button = (host: HTMLElement, label: string) =>
  Array.from(host.querySelectorAll("button")).find((x) => (x.textContent ?? "").includes(label)) as HTMLButtonElement | undefined;

describe("更新卡片", () => {
  it("没有 bridge（浏览器调试）时整块不渲染", async () => {
    (window as unknown as { ee?: unknown }).ee = {};
    const host = document.createElement("div");
    document.body.appendChild(host);
    await act(async () => {
      createRoot(host).render(<UpdateCard />);
    });
    expect(host.innerHTML).toBe("");
  });

  it("挂载时拉一次快照（窗口后打开也不会漏状态）", async () => {
    const { host, b } = await mount({ phase: "downloaded", current: "0.1.0", version: "0.2.0", message: "新版本 0.2.0 已就绪" });
    expect(b.state).toHaveBeenCalledTimes(1);
    expect(text(host)).toContain("新版本 0.2.0 已就绪");
    expect(button(host, "重启并安装")).toBeTruthy();
  });

  it.each([
    [{ phase: "idle", current: "0.1.0" } as UpdateState, "还没有检查过更新"],
    [{ phase: "checking", current: "0.1.0" } as UpdateState, "正在检查…"],
    [{ phase: "up-to-date", current: "0.1.0", message: "已是最新版本" } as UpdateState, "已是最新版本"],
    [{ phase: "downloading", current: "0.1.0", percent: 42.3 } as UpdateState, "42.3%"],
  ])("状态 %# 显示对应文案", async (st, want) => {
    const { host } = await mount(st);
    expect(text(host)).toContain(want);
  });

  it("检查失败要露出原因，不能静默", async () => {
    const { host } = await mount({ phase: "error", current: "0.1.0", message: "getaddrinfo ENOTFOUND github.com" });
    expect(text(host)).toContain("检查失败：getaddrinfo ENOTFOUND github.com");
  });

  it("点「检查更新」调一次 check", async () => {
    const { host, b } = await mount();
    const btn = button(host, "检查更新");
    expect(btn).toBeTruthy();
    await act(async () => btn!.click());
    expect(b.check).toHaveBeenCalledTimes(1);
    expect(text(host)).toContain("已是最新版本");
  });

  it("下载完成后按钮换成「重启并安装」，点击才调 install", async () => {
    const { host, b } = await mount();
    expect(button(host, "检查更新")).toBeTruthy();
    await act(async () => {
      b.push({ phase: "downloaded", current: "0.1.0", version: "0.2.0", percent: 100, message: "新版本 0.2.0 已下载" });
    });
    // 绝不自动重启：只是把按钮换出来
    expect(b.install).not.toHaveBeenCalled();
    const btn = button(host, "重启并安装");
    expect(btn).toBeTruthy();
    await act(async () => btn!.click());
    expect(b.install).toHaveBeenCalledTimes(1);
  });

  it("下载中只显示进度，不显示「检查更新」，避免中途重入", async () => {
    const { host } = await mount({ phase: "downloading", current: "0.1.0", percent: 7 });
    expect(button(host, "检查更新")).toBeUndefined();
    expect(button(host, "重启并安装")).toBeUndefined();
    expect(text(host)).toContain("7%");
  });

  it("卸载时退订，之后主进程再推状态也不报错", async () => {
    const b = bridge(base);
    (window as unknown as { ee?: unknown }).ee = { update: b };
    const host = document.createElement("div");
    document.body.appendChild(host);
    let root: ReturnType<typeof createRoot>;
    await act(async () => {
      root = createRoot(host);
      root.render(<UpdateCard />);
    });
    await act(async () => root!.unmount());
    // 退订后 push 不应再碰已卸载的组件（否则 React 会报 setState on unmounted）
    expect(() => b.push({ phase: "error", current: "0.1.0", message: "x" })).not.toThrow();
    expect(host.textContent).toBe("");
  });
});
