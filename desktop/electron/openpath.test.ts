// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { allowedRoots, decideOpen, guardedOpenPath, isExecutablePath } from "./openpath";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ee-openpath-"));
const home = path.join(tmp, "home");
const ws = path.join(tmp, "ws");
const outside = path.join(tmp, "outside");
for (const d of [home, ws, outside]) fs.mkdirSync(d, { recursive: true });
fs.writeFileSync(path.join(home, "desktop.json"), JSON.stringify({ workspaces: [{ path: ws, name: "ws" }] }));
const touch = (p: string) => (fs.writeFileSync(p, "x"), p);

describe("用默认程序打开：守门", () => {
  const roots = allowedRoots(home);

  it("工作区来自 desktop.json，外加 ee 目录", () => {
    expect(roots).toEqual([home, ws]);
  });

  it.each(["a.bat", "a.EXE", "a.cmd", "a.ps1", "a.vbs", "a.js", "a.lnk", "a.msi", "a.hta", "a.bat.", "a.cmd "])("可执行文件 %s 不运行，改为定位", (n) => {
    expect(isExecutablePath(n)).toBe(true);
  });

  it("普通文件照常打开", () => {
    const f = touch(path.join(ws, "readme.md"));
    expect(decideOpen(f, roots)).toEqual({ action: "open", path: f });
    expect(decideOpen(ws, roots).action).toBe("open"); // 目录在资源管理器里打开
  });

  it("工作区里的 .bat 只在资源管理器里定位", async () => {
    const f = touch(path.join(ws, "run.bat"));
    const sh = { openPath: vi.fn(async () => ""), showItemInFolder: vi.fn() };
    const msg = await guardedOpenPath(f, sh, roots);
    expect(sh.openPath).not.toHaveBeenCalled();
    expect(sh.showItemInFolder).toHaveBeenCalledWith(f);
    expect(msg).toContain("资源管理器");
  });

  it("工作区外、相对路径、网络路径、不存在的一律拒绝且不调用系统", async () => {
    const f = touch(path.join(outside, "x.txt"));
    const sh = { openPath: vi.fn(async () => ""), showItemInFolder: vi.fn() };
    for (const p of [f, "x.txt", "\\\\server\\share\\a.txt", path.join(ws, "..", "outside", "x.txt"), path.join(ws, "nope.txt"), "", 3]) {
      expect(await guardedOpenPath(p, sh, roots)).not.toBe("");
    }
    expect(sh.openPath).not.toHaveBeenCalled();
    expect(sh.showItemInFolder).not.toHaveBeenCalled();
  });

  it("主进程的 shell:openPath 走守门，不再直接 shell.openPath(p)", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.ts"), "utf-8");
    expect(main).not.toMatch(/"shell:openPath",\s*\(_e,\s*p:\s*string\)\s*=>\s*shell\.openPath\(p\)/);
    expect(main).toMatch(/"shell:openPath"[^\n]*guardedOpenPath/);
  });
});

describe("allowedRoots 把历史会话目录也算进白名单", () => {
  it("从 sessions/*.jsonl 的 cwd 收集根目录（否则看得见的项目点不开）", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "ee-home-"));
    const proj = fs.mkdtempSync(path.join(os.tmpdir(), "ee-proj-"));
    fs.mkdirSync(path.join(home, "sessions"), { recursive: true });
    // 会话首行带 cwd（真实格式）
    fs.writeFileSync(
      path.join(home, "sessions", "20261026-125138-20cba0.jsonl"),
      JSON.stringify({ type: "meta", cwd: proj, title: "t" }) + "\n",
      "utf-8",
    );
    const roots = allowedRoots(home);
    expect(roots).toContain(home);
    expect(roots, `白名单里没有会话目录 ${proj}：${JSON.stringify(roots)}`).toContain(proj);
    fs.rmSync(home, { recursive: true, force: true });
    fs.rmSync(proj, { recursive: true, force: true });
  });

  it("有 desktop.json 时两者合并，不互相覆盖", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "ee-home-"));
    const a = fs.mkdtempSync(path.join(os.tmpdir(), "ee-a-"));
    const b = fs.mkdtempSync(path.join(os.tmpdir(), "ee-b-"));
    fs.mkdirSync(path.join(home, "sessions"), { recursive: true });
    fs.writeFileSync(path.join(home, "desktop.json"), JSON.stringify({ workspaces: [{ path: a }] }), "utf-8");
    fs.writeFileSync(
      path.join(home, "sessions", "x.jsonl"),
      JSON.stringify({ cwd: b }) + "\n",
      "utf-8",
    );
    const roots = allowedRoots(home);
    expect(roots).toContain(a);
    expect(roots).toContain(b);
    for (const d of [home, a, b]) fs.rmSync(d, { recursive: true, force: true });
  });
});

describe("盘符根下的目录必须是绝对路径（用户报告的真实缺陷）", () => {
  // 实测 node 在 win32 上：path.isAbsolute("C:" + sep + "Users") === false，
  // 而更深一层的路径是 true。于是 D:<sep>ee日常 这种「盘符根目录」被误判成相对路径，
  // decideOpen 直接 deny —— 表现就是「点『在资源管理器中打开』没反应」。
  // 注意：vitest 的 jsdom 环境里 path 是 POSIX 实现，两者会不同；
  // 所以这里只断自定义实现的结果，不依赖 node 的行为。
  const D = "D:";
  const sep = "\\"; // 源码里就是一个反斜杠

  it("isAbsolutePath 识别盘符根目录", async () => {
    const { isAbsolutePath } = await import("./openpath");
    expect(isAbsolutePath(`${D}${sep}ee日常`), "盘符根目录应算绝对路径").toBe(true);
    expect(isAbsolutePath("d:/ee"), "正斜杠形式也应算").toBe(true);
    expect(isAbsolutePath(`${D}${sep}`), "只有 C:<sep> 也算").toBe(true);
    expect(isAbsolutePath(`${D}`), "光秃秃的盘符不算").toBe(false);
    expect(isAbsolutePath("relative" + sep + "path"), "相对路径不算").toBe(false);
  });

  it("decideOpen 对盘符根目录不再报「只能打开绝对路径」", () => {
    const root = `${D}${sep}ee日常`;
    const d = decideOpen(root, [root]);
    if (d.action === "deny") {
      expect(d.reason, `不该是路径格式问题，实际：${d.reason}`).not.toContain("绝对路径");
    }
    expect(d.action === "open" || d.action === "deny").toBe(true);
  });
});
