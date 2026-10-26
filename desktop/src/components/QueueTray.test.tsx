// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UserItem } from "../types";
import { QueueSendButton, QueueTray } from "./QueueTray";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

function render(node: React.ReactNode) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  act(() => createRoot(el).render(<TT.Provider>{node}</TT.Provider>));
  return el;
}

const q = (id: string, text: string, queue: "steer" | "follow") => ({ id, type: "user", text, queued: true, queue }) as UserItem;

describe("QueueTray", () => {
  it("按顺序显示插话 / 待追加标签，点撤回回调条目 id", () => {
    const onWithdraw = vi.fn();
    const el = render(<QueueTray items={[q("a", "顺便看看 b.txt", "steer"), q("b", "完成后写总结", "follow")]} onWithdraw={onWithdraw} />);
    const rows = el.querySelectorAll("[data-queue]");
    expect(rows.length).toBe(2);
    expect(rows[0].getAttribute("data-queue")).toBe("steer");
    expect(rows[0].textContent).toContain("插话");
    expect(rows[0].textContent).toContain("顺便看看 b.txt");
    expect(rows[1].textContent).toContain("待追加");
    act(() => (rows[1].querySelector("button[aria-label='撤回']") as HTMLButtonElement).click());
    expect(onWithdraw).toHaveBeenCalledWith("b");
  });

  it("没有排队消息时不渲染", () => {
    const el = render(<QueueTray items={[]} onWithdraw={() => undefined} />);
    expect(el.querySelector("[data-testid='queue-tray']")).toBeNull();
  });

  it("运行中的发送按钮：主按钮 = 插话", () => {
    const onSend = vi.fn();
    const el = render(<QueueSendButton disabled={false} onSend={onSend} />);
    act(() => (el.querySelector("button[aria-label='插话']") as HTMLButtonElement).click());
    expect(onSend).toHaveBeenCalledWith("steer");
  });
});
