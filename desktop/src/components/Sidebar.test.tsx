// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import { Sidebar } from "./Sidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: { unmount(): void }[] = [];
afterEach(async () => {
  for (const r of roots.splice(0)) await act(async () => r.unmount());
  document.body.innerHTML = "";
});

const searchBox = () => document.querySelector<HTMLInputElement>('input[placeholder="搜索对话标题和内容"]');

describe("侧栏 Ctrl K", () => {
  it("按 Ctrl+K 打开并聚焦对话搜索框；再按一次仍聚焦（选中已有内容）", async () => {
    useApp.setState({ rpc: null, threads: [], workspaces: [], search: "", view: "settings" } as never);
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    roots.push(root);
    await act(async () => root.render(<TT.Provider><Sidebar /></TT.Provider>));
    expect(searchBox()).toBeNull();
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true }));
    });
    expect(searchBox()).not.toBeNull();
    expect(document.activeElement).toBe(searchBox());
    (document.activeElement as HTMLElement).blur();
    const ev = new KeyboardEvent("keydown", { key: "K", ctrlKey: true, bubbles: true, cancelable: true });
    await act(async () => {
      window.dispatchEvent(ev);
    });
    expect(ev.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(searchBox());
  });
});
