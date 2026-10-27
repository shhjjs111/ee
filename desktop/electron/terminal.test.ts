// @vitest-environment node
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { beforeAll, describe, expect, it, vi } from "vitest";

const handlers = new Map<string, (...a: any[]) => any>();
vi.mock("electron", () => ({
  ipcMain: {
    handle: (ch: string, fn: any) => handlers.set(ch, fn),
    on: (ch: string, fn: any) => handlers.set(ch, fn),
  },
}));

// 假 node-pty：记录 kill
const fake = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "ee-pty-")), "fake-pty.cjs");
fs.writeFileSync(
  fake,
  `const killed = []; module.exports = { killed, spawn() { const p = { pid: 1000 + killed.length, killed: false,
    onData() { return { dispose() {} }; }, onExit(cb) { p._exit = cb; return { dispose() {} }; },
    write() {}, resize() {}, kill() { p.killed = true; killed.push(p.pid); } }; return p; } };`,
);

class FakeSender extends EventEmitter {
  destroyed = false;
  isDestroyed() {
    return this.destroyed;
  }
  send() {}
}

let T: typeof import("./terminal");
beforeAll(async () => {
  T = await import("./terminal");
  T.setPtySearchPaths([fake]);
  T.registerTerminalIpc();
});

function create(sender: FakeSender) {
  return handlers.get("term:create")!({ sender }, { cwd: os.tmpdir() });
}

describe("内置终端的生命周期", () => {
  it("同一个窗口开很多终端也只挂一个 destroyed 监听（不泄漏、不触发 MaxListeners 警告）", () => {
    const s = new FakeSender();
    for (let i = 0; i < 15; i++) expect(create(s).ok).toBe(true);
    expect(s.listenerCount("destroyed")).toBeLessThanOrEqual(1);
    s.emit("destroyed");
  });

  it("页面重新加载（渲染进程换了一份）时结束旧页面的终端，不留孤儿 shell", () => {
    const s = new FakeSender();
    const a = create(s);
    const b = create(s);
    expect(a.ok && b.ok).toBe(true);
    s.emit("did-start-navigation", { isMainFrame: true, isSameDocument: false });
    // 旧终端已经不在了：再写 / 杀返回 false
    expect(handlers.get("term:kill")!({}, a.id)).toBe(false);
    expect(handlers.get("term:kill")!({}, b.id)).toBe(false);
  });

  it("页面内的锚点跳转不影响终端", () => {
    const s = new FakeSender();
    const a = create(s);
    s.emit("did-start-navigation", { isMainFrame: true, isSameDocument: true });
    expect(handlers.get("term:kill")!({}, a.id)).toBe(true);
  });
});
