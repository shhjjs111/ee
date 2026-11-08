// 拖入/粘贴非图片文件：输入框里要出现它的真实路径（用户报告的「发不出去」）。
//
// 为什么必须上真实 Electron：jsdom 里 `File` 是 JS 造的，没有磁盘对应，
// `webUtils.getPathForFile` 只会返回空串——「File 能不能跨 contextBridge 传给
// preload」这件事 jsdom 根本验不了。这里用 `setInputFiles` 塞**磁盘上真实存在的文件**，
// 走完 input change → addFiles → window.ee.getFilePath → preload 的 webUtils 全链路。
//
// 运行前提：`npm run build`（Electron 加载 dist/，不重建看不到源码改动）。

import { expect, test, type ElectronApplication, type Page } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { launchIsolated, type IsolatedApp } from "./launch";

let app: ElectronApplication;
let iso: IsolatedApp;
let win: Page;
let dir = "";

test.beforeAll(async () => {
  iso = await launchIsolated();
  app = iso.app;
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "ee-attach-"));
});

test.afterAll(async () => {
  await iso?.dispose();
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    /* Windows 偶发占用，删不掉不影响断言 */
  }
});

/** Composer 的输入框（空闲态 placeholder 固定）。 */
const box = () => win.locator('textarea[placeholder*="让 ee 帮你写代码"]');
const fileInput = () => win.locator('input[type="file"]');

test("选择磁盘上的 PDF：输入框里出现它的绝对路径", async () => {
  const pdf = path.join(dir, "论文.pdf");
  fs.writeFileSync(pdf, "%PDF-1.4\n%%EOF\n");

  await fileInput().setInputFiles(pdf);

  // 含空格时前端会加引号包住，所以断言「包含完整路径」对两种形态都成立
  await expect
    .poll(async () => box().inputValue(), { timeout: 10_000 })
    .toContain(pdf);
  expect(await box().inputValue(), "路径要以 @ 引用形态插进去").toContain("@");
});

test("读不了的类型（zip）：输入框不动，并给出说明", async () => {
  const zip = path.join(dir, "资料.zip");
  fs.writeFileSync(zip, "PK\u0003\u0004");

  const before = await box().inputValue();
  await fileInput().setInputFiles(zip);

  await expect(win.getByText(/资料\.zip/).first(), "zip 这类文件必须给提示，不能静默").toBeVisible({
    timeout: 10_000,
  });
  expect(await box().inputValue(), "压缩包不该被插进输入框").toBe(before);
});
