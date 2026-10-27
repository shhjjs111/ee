// @vitest-environment node
import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Backend, backendCommand, type BackendOptions } from "./backend";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ee-backend-test-"));

function script(name: string, body: string): string {
  const p = path.join(tmp, name);
  fs.writeFileSync(p, body);
  return p;
}

// 正常的假内核：打印 READY，stdin 关闭就退出
const GOOD = script(
  "good.js",
  `process.stdout.write('EE_SERVE_READY {"port": 1234}\\n'); process.stdin.on('data', () => {}); process.stdin.on('end', () => process.exit(0)); process.stdin.resume();`,
);
// 不理 stdin EOF 的假内核（模拟事件循环卡死）
const STUCK = script("stuck.js", `process.stdout.write('EE_SERVE_READY {"port": 1}\\n'); setInterval(() => {}, 1000);`);
// 永远不 READY
const SILENT = script("silent.js", `setInterval(() => {}, 1000);`);
const FAIL = script("fail.js", `process.exit(3);`);

function opts(extra: Partial<BackendOptions> = {}): BackendOptions {
  return { isPackaged: false, resourcesPath: tmp, appRoot: tmp, logFile: path.join(tmp, "logs", "backend.log"), ...extra };
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitFor(pred: () => boolean, ms = 5000): Promise<boolean> {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (pred()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return pred();
}

const node = (file: string) => () => ({ cmd: process.execPath, args: [file], cwd: tmp, env: {} });

describe("Backend（主进程拉起内核）", () => {
  it("命令行带上 --parent-pid，内核能在桌面端被强杀后自己退出", () => {
    const c = backendCommand(opts());
    expect(c.args).toContain("--exit-on-stdin-eof");
    const i = c.args.indexOf("--parent-pid");
    expect(i).toBeGreaterThan(-1);
    expect(c.args[i + 1]).toBe(String(process.pid));
  });

  it("重启时旧进程退出不当成“意外退出”（否则界面会再自动重启一次）", async () => {
    const b = new Backend(opts({ command: node(GOOD) }));
    const exits: (number | null)[] = [];
    b.onExit = (code) => exits.push(code);
    const first = await b.info();
    const second = await b.restart();
    expect(second.pid).not.toBe(first.pid);
    await waitFor(() => !alive(first.pid));
    await new Promise((r) => setTimeout(r, 200));
    expect(exits).toEqual([]);
    b.stop();
    await waitFor(() => !alive(second.pid));
  });

  it("启动失败后再次 info() 会重新启动，而不是一直返回同一个失败", async () => {
    let file = FAIL;
    const b = new Backend(opts({ command: () => node(file)() }));
    await expect(b.info()).rejects.toThrow(/退出码 3/);
    file = GOOD;
    const r = await b.info();
    expect(r.port).toBe(1234);
    b.stop();
    await waitFor(() => !alive(r.pid));
  });

  it("启动超时会结束那个进程，不留后台残留", async () => {
    const b = new Backend(opts({ command: node(SILENT), startTimeoutMs: 400 }));
    let pid = 0;
    const orig = b.info();
    await expect(orig).rejects.toThrow(/没有启动/);
    pid = b.lastPid;
    expect(pid).toBeGreaterThan(0);
    expect(await waitFor(() => !alive(pid))).toBe(true);
  });

  it("内核不响应 stdin EOF 时，宽限期后强制结束", async () => {
    const b = new Backend(opts({ command: node(STUCK), stopGraceMs: 300 }));
    const r = await b.info();
    b.stop();
    expect(await waitFor(() => !alive(r.pid), 6000)).toBe(true);
  });
});

describe("Backend.stopAndWait（退出应用）", () => {
  it("正常内核：等它自己退出", async () => {
    const b = new Backend(opts({ command: node(GOOD) }));
    const r = await b.info();
    const t0 = Date.now();
    await b.stopAndWait(3000);
    expect(alive(r.pid)).toBe(false);
    expect(Date.now() - t0).toBeLessThan(2500);
  });

  it("卡死的内核：超时后同步结束", async () => {
    const b = new Backend(opts({ command: node(STUCK), stopGraceMs: 60000 }));
    const r = await b.info();
    await b.stopAndWait(300);
    expect(await waitFor(() => !alive(r.pid), 2000)).toBe(true);
  });
});
