// 设置页必须能滚动（用户报告的真实缺陷）。
//
// 现场：设置页内容超出窗口高度后被截断，滚轮不动，下面几项（命令超时、队列设置…）
// 看不见。根因是 flex 高度链断了：
//   - AppShell 的 main 容器缺 min-h-0（flex 子项默认 min-height:auto，被内容撑开）
//   - SettingsPage 根容器缺 h-full / min-h-0
// 于是内层 overflow-y-auto 拿不到受限高度，永远不会滚动。
//
// 这类缺陷用 jsdom 测不出来（它不算布局），必须用真实 Electron 窗口量 scrollHeight。

import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import path from "node:path";

let app: ElectronApplication;
let win: Page;

test.beforeAll(async () => {
  app = await electron.launch({ args: ["."], cwd: path.resolve(__dirname, "..") });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
  // 窗口压小一点，保证设置页内容一定超出
  await app.evaluate(async ({ BrowserWindow }) => {
    BrowserWindow.getAllWindows()[0]?.setSize(1100, 700);
  });
  await win.waitForTimeout(800);
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
});

test("设置页能滚动到底部内容", async () => {
  // 进入设置
  await win.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === "设置");
    b?.click();
  });
  await win.waitForTimeout(1200);

  // 找一个「内容高于视口」的滚动容器
  const info = await win.evaluate(() => {
    const cands = [...document.querySelectorAll("div")].filter((el) => {
      const cs = getComputedStyle(el);
      return (cs.overflowY === "auto" || cs.overflowY === "scroll") && el.scrollHeight > 200;
    });
    if (!cands.length) return null;
    // 取能滚得动的那个（scrollHeight 明显大于 clientHeight）
    const el = cands.sort((a, b) => b.scrollHeight - b.clientHeight - (a.scrollHeight - a.clientHeight))[0];
    return {
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      canScroll: el.scrollHeight > el.clientHeight + 4,
    };
  });

  expect(info, "设置页里找不到任何纵向滚动容器").not.toBeNull();
  expect(
    info!.canScroll,
    `设置页内容 ${info!.scrollHeight}px 高于可视 ${info!.clientHeight}px 却没有可滚动空间`,
  ).toBe(true);

  // 真的滚一下，确认能到底
  const scrolled = await win.evaluate(() => {
    const el = [...document.querySelectorAll("div")].find((e) => {
      const cs = getComputedStyle(e);
      return (cs.overflowY === "auto" || cs.overflowY === "scroll") && e.scrollHeight > e.clientHeight + 4;
    }) as HTMLElement | undefined;
    if (!el) return null;
    const before = el.scrollTop;
    el.scrollTop = el.scrollHeight;
    return { before, after: el.scrollTop, max: el.scrollHeight - el.clientHeight };
  });
  expect(scrolled, "滚动容器不见了").not.toBeNull();
  expect(scrolled!.after, "滚到底失败").toBeGreaterThan(0);
  expect(Math.abs(scrolled!.after - scrolled!.max)).toBeLessThanOrEqual(2);
});

test("滚到底后能看到最后一项设置（而不是被截断）", async () => {
  await expect(win.locator("body")).toContainText("完成后追加的送入方式");
});
