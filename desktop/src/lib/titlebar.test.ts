// 标题栏高度的单一来源（Wave 4 收口）。
//
// 改版过程中发现：Titlebar.tsx 建好了却没有任何消费者（42 行死代码），
// 各页面仍各自写 h-11 + 手写 padding。这类"建了没接"的东西最容易被当成已完成。
// 这里钉住两件事：常量本身的值，以及各页面确实从这个常量取值。
//
// 用 vite 的 ?raw 读源码文本：渲染层 tsconfig 不含 node 类型，
// 直接 import node:fs 会让 typecheck 失败（实测 TS2307）。
import { describe, expect, it } from "vitest";
import { TITLEBAR_H } from "../components/shell/Titlebar";
import conversationSrc from "../components/Conversation.tsx?raw";
import sidebarSrc from "../components/Sidebar.tsx?raw";
import settingsSrc from "../components/SettingsPage.tsx?raw";
import mainSrc from "../../electron/main.ts?raw";

describe("标题栏高度只有一个来源", () => {
  it("常量是 44px（与 Electron titleBarOverlay 高度一致）", () => {
    expect(TITLEBAR_H).toBe(44);
  });

  it("会话标题栏、侧栏头部、设置页都从常量取高度，不再硬编码 h-11", () => {
    const files: [string, string][] = [
      ["Conversation.tsx", conversationSrc],
      ["Sidebar.tsx", sidebarSrc],
      ["SettingsPage.tsx", settingsSrc],
    ];
    for (const [name, src] of files) {
      expect(src, `${name} 应引入 TITLEBAR_H`).toContain("TITLEBAR_H");
      expect(src, `${name} 不应再写 drag + h-11 作为标题栏高度`).not.toMatch(/drag[^"]*h-11/);
    }
  });

  it("主进程的 overlay 高度与前端常量对齐（防止两边各改各的）", () => {
    const m = /height:\s*(\d+)/.exec(mainSrc);
    expect(m, "主进程里应有 titleBarOverlay 高度").toBeTruthy();
    expect(Number(m![1])).toBe(TITLEBAR_H);
  });
});
