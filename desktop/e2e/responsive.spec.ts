// 多分辨率 / 高 DPI 的布局矩阵（Wave 6）。
//
// 之前只在 125%（本机默认）下看过。这里覆盖 3 个窗口尺寸 × 布局关键量，
// 并且用 Electron 的 zoomFactor 逼近更高缩放——注意这是**代理**：
// 真正的 150% 系统 DPI 还要看系统级缩放，测试里只能验证布局在等效缩放下不塌。
//
// 判定一律用几何量：中区宽度、输入框宽度、标题栏高度、是否出现横向溢出。

import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import path from "node:path";

let app: ElectronApplication;
let win: Page;

test.beforeAll(async () => {
  app = await electron.launch({ args: ["."], cwd: path.resolve(__dirname, "..") });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
  await win.evaluate(() => {
    localStorage.removeItem("ee.layout");
    localStorage.removeItem("ee.right");
  });
  await win.reload();
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
});

async function resize(w: number, h: number) {
  await app.evaluate(async ({ BrowserWindow }, size) => {
    const win = BrowserWindow.getAllWindows()[0];
    win?.setSize(size.w, size.h);
  }, { w, h });
  await win.waitForTimeout(700);
}

async function metrics() {
  return win.evaluate(() => {
    const shell = document.querySelector("[data-testid=app-shell]") as HTMLElement | null;
    const main = shell?.children[1] as HTMLElement | undefined;
    const ta = document.querySelector("textarea") as HTMLTextAreaElement | null;
    const sb = document.querySelector("[data-testid=shell-sidebar]") as HTMLElement | null;
    const clip = document.documentElement;
    return {
      innerWidth: window.innerWidth,
      mainW: main ? Math.round(main.getBoundingClientRect().width) : 0,
      taW: ta ? Math.round(ta.getBoundingClientRect().width) : 0,
      sidebarW: sb ? Math.round(sb.getBoundingClientRect().width) : 0,
      // 横向溢出：整页是否出现横向滚动条
      overflowX: clip.scrollWidth > clip.clientWidth + 1,
      headerH: (() => {
        const h = [...document.querySelectorAll("div")].find((e) => {
          const c = (e.className || "").toString();
          return c.includes("drag") && c.includes("border-b");
        });
        return h ? Math.round(h.getBoundingClientRect().height) : null;
      })(),
    };
  });
}

for (const [label, w, h] of [
  ["960×600 最小窗口", 960, 600],
  ["1360×860 默认窗口", 1360, 860],
  ["1920×1080 大屏", 1920, 1080],
] as const) {
  test(`${label}：中区与输入框宽度合理，无横向溢出`, async () => {
    await resize(w, h);
    const m = await metrics();

    // 中区至少 400px 才谈得上能读代码
    expect(m.mainW, `中区 ${m.mainW}px 过窄`).toBeGreaterThan(400);
    expect(m.taW, `输入框 ${m.taW}px 过窄`).toBeGreaterThan(300);
    // 侧栏宽度应被夹在合法范围
    expect(m.sidebarW).toBeGreaterThanOrEqual(200);
    expect(m.sidebarW).toBeLessThanOrEqual(300);
    // 标题栏高度不受窗口尺寸影响
    expect(m.headerH).toBe(44);
    // 不允许整页横向滚动
    expect(m.overflowX, "出现整页横向滚动条").toBe(false);
  });
}

test("150% 等效缩放（zoomFactor 1.5）：布局不塌", async () => {
  await resize(1360, 860);
  // 用 zoomFactor 逼近 150%：内容等效放大，等效视口变小
  await app.evaluate(async ({ BrowserWindow }) => {
    const win = BrowserWindow.getAllWindows()[0];
    win?.webContents.setZoomFactor(1.5);
  });
  await win.waitForTimeout(900);

  const m = await metrics();
  // 缩放后 CSS 像素变少，但仍须可用
  expect(m.mainW).toBeGreaterThan(400);
  expect(m.taW).toBeGreaterThan(300);
  expect(m.overflowX, "150% 下出现整页横向滚动").toBe(false);

  // 还原，避免影响后续
  await app.evaluate(async ({ BrowserWindow }) => {
    const win = BrowserWindow.getAllWindows()[0];
    win?.webContents.setZoomFactor(1);
  });
});

test("125%（本机默认 DPI）：关键量仍正确", async () => {
  await resize(1360, 860);
  const m = await metrics();
  expect(m.headerH).toBe(44);
  expect(m.mainW).toBeGreaterThan(600);
  expect(m.overflowX).toBe(false);
});
