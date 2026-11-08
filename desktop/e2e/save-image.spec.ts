// 另存为：真实 Electron 里的 IPC + 写盘。
//
// 为什么必须真机：jsdom 能验「点了按钮、参数传对了」，但验不了
// 「preload → ipcMain → dialog → fs.writeFile」这条链路真的把字节写到了磁盘上。
// 保存对话框本身无法自动化，所以用 app.evaluate 在主进程里把 showSaveDialog
// 换成一个固定路径的桩——被测的其余部分（invoke 通道、数据校验、写文件）全是真代码。

import { expect, test, type ElectronApplication, type Page } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { launchIsolated, type IsolatedApp } from "./launch";

let app: ElectronApplication;
let iso: IsolatedApp;
let win: Page;
let dir = "";

// 1x1 的 PNG（真实文件头，能通过后端的 MIME 嗅探逻辑）
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
);
const DATA_URL = "data:image/png;base64," + PNG.toString("base64");

test.beforeAll(async () => {
  iso = await launchIsolated();
  app = iso.app;
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "ee-saveimg-"));
});

test.afterAll(async () => {
  await iso?.dispose();
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    /* Windows 偶发占用 */
  }
});

test("window.ee.saveImage 把原图字节写到用户选的路径", async () => {
  const dest = path.join(dir, "saved.png");
  await app.evaluate(({ dialog }, p) => {
    // 真对话框无法在自动化里点掉，换成立刻返回固定路径的桩
    (dialog as unknown as { showSaveDialog: unknown }).showSaveDialog = async () => ({ canceled: false, filePath: p });
  }, dest);

  const r = (await win.evaluate((d) => window.ee!.saveImage!("截图.png", d), DATA_URL)) as {
    ok: boolean;
    path?: string;
    error?: string;
  };

  expect(r.error, "写盘报错了").toBeUndefined();
  expect(r.ok).toBe(true);
  expect(r.path).toBe(dest);
  expect(fs.existsSync(dest), "文件没有被真的创建").toBe(true);
  // 字节必须与 data URL 里的完全一致——转码错了会写出坏图
  expect(fs.readFileSync(dest).equals(PNG)).toBe(true);
});

test("不是图片的数据一律拒收，不落地", async () => {
  const dest = path.join(dir, "bad.png");
  await app.evaluate(({ dialog }, p) => {
    (dialog as unknown as { showSaveDialog: unknown }).showSaveDialog = async () => ({ canceled: false, filePath: p });
  }, dest);

  const r = (await win.evaluate(() => window.ee!.saveImage!("x.png", "data:text/plain;base64,aGVsbG8="))) as {
    ok: boolean;
    error?: string;
  };

  expect(r.ok).toBe(false);
  expect(r.error).toContain("图片");
  expect(fs.existsSync(dest), "被拒的数据不该写出文件").toBe(false);
});
