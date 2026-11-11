// 会话写盘失败要在**真实窗口**里看得见（磁盘满 / 会话文件被别的程序占用）。
//
// jsdom 用例（src/components/SaveErrorBanner.test.tsx）只证明字段接进了组件；
// 这里补真机这一层：横幅真的挂出来、没有被滚动区挤掉，左栏行上也有标记。
//
// 制造失败的方式与真实故障同一条路径：把 `<EE_HOME>/sessions` 换成同名普通文件，
// 后端下一次 append 就 OSError（mkdir 失败）——不 mock 后端，走真 WS 真进程。
import { expect, test, type ElectronApplication, type Page } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { launchIsolated, type IsolatedApp } from "./launch";
import { pageRpc } from "./rpc";

const TITLE = "e2e-写盘失败";

let app: ElectronApplication;
let iso: IsolatedApp;
let win: Page;
let tid = "";
let projDir = "";

test.beforeAll(async () => {
  iso = await launchIsolated();
  app = iso.app;
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
  await win.waitForTimeout(800);

  projDir = fs.mkdtempSync(path.join(os.tmpdir(), "ee-e2e-savefail-"));
  const started = await pageRpc<{ thread: { id: string } }>(win, "thread/start", { cwd: projDir });
  tid = started.thread.id;
  await pageRpc(win, "thread/rename", { thread_id: tid, title: TITLE });
  await win.reload();
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });

  const row = win.locator(`[data-testid=thread-row][data-title="${TITLE}"]`);
  await row.waitFor({ state: "visible", timeout: 30_000 });
  await row.click();
  await win.locator("[data-testid=conversation-scroller]").waitFor({ state: "visible", timeout: 30_000 });
});

test.afterAll(async () => {
  await iso?.dispose();
  try {
    fs.rmSync(projDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

test("磁盘写不进去：对话上方出现横幅，左栏行上有标记", async () => {
  const sessions = path.join(iso.home, "sessions");
  fs.rmSync(sessions, { recursive: true, force: true });
  fs.writeFileSync(sessions, "这不是目录", "utf8");

  // 真写失败：改标题会 save_meta（与用户改会话名同一条路径）
  await pageRpc(win, "thread/rename", { thread_id: tid, title: `${TITLE}-没存上` });

  const banner = win.locator("[data-testid=save-error-banner]");
  await expect(banner).toBeVisible({ timeout: 10_000 });
  await expect(banner).toContainText("没能保存到磁盘");

  // 横幅要在视口内（结构上是 Header 之下的 flex 子项，不该被推出去）
  const box = await banner.boundingBox();
  const viewport = win.viewportSize();
  expect(box, "横幅没有布局盒").not.toBeNull();
  expect(box!.height, "横幅高度为 0，等于没显示").toBeGreaterThan(10);
  if (viewport) expect(box!.y).toBeLessThan(viewport.height);

  // 左栏那一行也要有标记：用户不在这个会话里时也能发现「保存不了」
  await expect(win.locator("[data-testid=thread-save-error]").first()).toBeVisible();

  // 对话里还有一条可读的说明（横幅只说明现状，这条告诉用户丢了什么）。
  // 注意局限在滚动区里：横幅自己也有同样的字，不限定会 strict mode 冲突。
  const notice = win.locator("[data-testid=conversation-scroller] >> text=磁盘可能已满");
  await expect(notice.first()).toBeVisible();
});
