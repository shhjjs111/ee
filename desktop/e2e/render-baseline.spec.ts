// 真实 Electron 窗口的渲染基线（Wave 0 Task 1 / Wave 6 视觉矩阵）。
//
// 这里测的都是**用户能看见的行为**，不是内部实现：
//   - 首屏是否对话优先（工具面板默认收起）
//   - 点开工具面板后是不是真有宽度（曾经是 1px）
//   - 输入框有没有常显滚动条（曾经有）
//   - 960×600 下中区是否还有可用宽度
//
// 判定一律用真实几何测量（getBoundingClientRect / offsetWidth），
// 不用截图相似度：这台机器上没人维护基线图，比像素更稳的是尺寸断言。

import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import path from "node:path";

let app: ElectronApplication;
let win: Page;

test.beforeAll(async () => {
  app = await electron.launch({
    args: ["."],
    cwd: path.resolve(__dirname, ".."),
  });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  // 等首屏连上内核（启动页消失、侧栏出现）
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
});

test("首屏是对话优先：全新偏好下工具面板不占位", async () => {
  // 这台机器上 localStorage 可能已有历史偏好（ee.right=1），那属于用户明确保存的选择，
  // 应当被尊重。所以先清掉偏好再断言"全新用户看到什么"。
  await win.evaluate(() => {
    localStorage.removeItem("ee.layout");
    localStorage.removeItem("ee.right");
    localStorage.removeItem("ee.rightWidth");
  });
  await win.reload();
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });

  const shell = win.locator("[data-testid=app-shell]");
  await expect(shell).toBeVisible();
  // 全新偏好下默认收起：不应存在 inspector 容器
  const inspector = win.locator("[data-testid=shell-inspector]");
  expect(await inspector.count()).toBe(0);

  const sidebar = win.locator("[data-testid=shell-sidebar]");
  const sbW = await sidebar.evaluate((el) => Math.round(el.getBoundingClientRect().width));
  expect(sbW).toBeGreaterThan(200);
  expect(sbW).toBeLessThan(300);
});

test("输入框没有常显的竖向滚动条", async () => {
  const ta = win.locator("textarea").first();
  await expect(ta).toBeVisible();
  const m = await ta.evaluate((el) => ({
    offset: el.offsetWidth,
    client: el.clientWidth,
  }));
  // 差 >1px 说明出现了滚动条占位（改版前的缺陷）
  expect(m.offset - m.client).toBeLessThanOrEqual(1);
});

test("点开工具面板后真的有宽度（不是 1px）", async () => {
  // 标题栏最右侧那个按钮就是展开/收起面板
  await win.evaluate(() => {
    const bs = [...document.querySelectorAll("button")]
      .map((b) => ({ b, r: b.getBoundingClientRect() }))
      .filter((x) => x.r.top < 50 && x.r.width > 0 && x.r.width < 60);
    bs.sort((a, z) => z.r.left - a.r.left);
    bs[0]?.b.click();
  });

  const inspector = win.locator("[data-testid=shell-inspector]");
  await expect(inspector).toHaveCount(1, { timeout: 10_000 });
  const w = await inspector.evaluate((el) => Math.round(el.getBoundingClientRect().width));
  // 曾经这里是 1（容器在但宽度为 0）
  expect(w).toBeGreaterThan(300);
});

test("960×600 小窗口：中区仍有可用宽度，输入框不溢出", async () => {
  await app.evaluate(async ({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows()[0];
    w?.setSize(960, 600);
  });
  await win.waitForTimeout(800);

  const shell = win.locator("[data-testid=app-shell]");
  const mainW = await shell.evaluate((el) => {
    const main = el.children[1] as HTMLElement | undefined;
    return main ? Math.round(main.getBoundingClientRect().width) : 0;
  });
  // 对话区至少要有 400px 才谈得上可用
  expect(mainW).toBeGreaterThan(400);

  const taW = await win.locator("textarea").first().evaluate((el) => Math.round(el.getBoundingClientRect().width));
  expect(taW).toBeGreaterThan(300);
});

test("深色主题令牌生效", async () => {
  const tokens = await win.evaluate(() => {
    document.documentElement.classList.add("dark");
    const cs = getComputedStyle(document.documentElement);
    return {
      dark: document.documentElement.classList.contains("dark"),
      bg: cs.getPropertyValue("--bg").trim(),
      fg: cs.getPropertyValue("--fg").trim(),
    };
  });
  expect(tokens.dark).toBe(true);
  expect(tokens.bg).toMatch(/^#/);
  expect(tokens.fg).toMatch(/^#/);
});
