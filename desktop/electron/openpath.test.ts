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
