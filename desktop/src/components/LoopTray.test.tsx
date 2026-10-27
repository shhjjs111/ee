// 会话内 /loop 托盘（D5-3）：列出循环、等待中的状态、停止、转为自动化。
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionLoop } from "../lib/loop";
import * as Tooltip from "@radix-ui/react-tooltip";
import { LoopTray } from "./LoopTray";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

const lp: SessionLoop = {
  id: "3",
  thread_id: "t1",
  prompt: "检查CI是否通过",
  schedule: { kind: "interval", minutes: 10 },
  schedule_text: "每 10 分钟",
  created: 0,
  next_run: null,
  last_run: null,
  runs: 2,
  skipped: 0,
  pending: true,
};

describe("LoopTray", () => {
  it("没有循环时不渲染", () => {
    act(() => root.render(<LoopTray loops={[]} onStop={() => undefined} onConvert={() => undefined} />));
    expect(host.querySelector("[data-testid=loop-tray]")).toBeNull();
  });

  it("显示循环并能停止、转为自动化", () => {
    const onStop = vi.fn();
    const onConvert = vi.fn();
    act(() => root.render(<Tooltip.Provider><LoopTray loops={[lp]} onStop={onStop} onConvert={onConvert} /></Tooltip.Provider>));
    const tray = host.querySelector("[data-testid=loop-tray]")!;
    expect(tray.textContent).toContain("#3 每 10 分钟");
    expect(tray.textContent).toContain("等这一回合结束后发送");
    act(() => (host.querySelector("[aria-label=停止循环]") as HTMLButtonElement).click());
    expect(onStop).toHaveBeenCalledWith("3");
    act(() => (host.querySelector("[aria-label=转为自动化]") as HTMLButtonElement).click());
    expect(onConvert).toHaveBeenCalledWith(lp);
  });
});
