// 深色主题的全页面检查（Wave 6 视觉矩阵）。
//
// 之前只验了「令牌值对不对」（--bg 是不是 #181818），没验「每个页面在深色下能不能读」。
// 这个差别很实在：令牌对但某个组件写死了 bg-white，深色下就是一块刺眼的白。
//
// 判定用可测量的量，不是看图：
//   - 页面主区域的实际背景色必须接近深色令牌
//   - 不能出现「大面积近白背景」（写死颜色的典型症状）
//   - 正文文字与背景的对比度要够（用 luminance 算，不靠眼估）

import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import path from "node:path";

let app: ElectronApplication;
let win: Page;

/** 在页面里跑：把颜色转成相对亮度，算对比度。支持 #rgb / #rrggbb / rgb() / rgba()。 */
const CONTRAST_FN = `
function rgbOf(c) {
  if (!c) return null;
  c = c.trim();
  if (c.startsWith("#")) {
    const h = c.slice(1);
    if (h.length === 3) return h.split("").map((x) => parseInt(x + x, 16));
    if (h.length >= 6) return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    return null;
  }
  const m = c.match(/[\\d.]+/g);
  if (!m || m.length < 3) return null;
  return m.slice(0, 3).map(Number);
}
function lum(c) {
  const rgb = rgbOf(c);
  if (!rgb) return null;
  const [r, g, b] = rgb.map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a, b) {
  const la = lum(a), lb = lum(b);
  if (la === null || lb === null) return null;
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
`;

test.beforeAll(async () => {
  app = await electron.launch({ args: ["."], cwd: path.resolve(__dirname, "..") });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
  // 固定为深色，并清掉偏好让首屏是对话优先
  await win.evaluate(() => {
    localStorage.removeItem("ee.layout");
    localStorage.removeItem("ee.right");
    localStorage.setItem("ee.theme", "dark");
    document.documentElement.classList.add("dark");
  });
  await win.waitForTimeout(600);
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
});

test("深色下主背景是深色，且没有大面积写死的近白块", async () => {
  const r = await win.evaluate(`(() => {
    ${CONTRAST_FN}
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    const body = getComputedStyle(document.body).backgroundColor;
    // 扫一遍可见的大块元素，找有没有近白背景（深色主题下这是 bug）
    const nearWhite = [];
    for (const el of document.querySelectorAll("main, aside, section, div")) {
      const box = el.getBoundingClientRect();
      if (box.width * box.height < 200 * 100) continue;   // 只看大面积
      const c = getComputedStyle(el).backgroundColor;
      if (!c || c === "rgba(0, 0, 0, 0)" || c === "transparent") continue;
      const l = lum(c);
      if (l !== null && l > 0.6) nearWhite.push({ cls: (el.className || "").toString().slice(0, 50), c, area: Math.round(box.width * box.height) });
    }
    return { bg, body, nearWhite: nearWhite.slice(0, 5) };
  })()`);

  expect(r.bg).toBe("#181818");
  expect(r.nearWhite, `深色主题下出现近白大块：${JSON.stringify(r.nearWhite)}`).toHaveLength(0);
});

test("会话页正文对比度达标（≥ 4.5:1）", async () => {
  const r = await win.evaluate(`(() => {
    ${CONTRAST_FN}
    const root = getComputedStyle(document.documentElement);
    const fg = root.getPropertyValue("--fg").trim();
    const bg = root.getPropertyValue("--bg").trim();
    return { ratio: contrast(fg, bg), fg, bg };
  })()`);
  expect(r.ratio).not.toBeNull();
  expect(r.ratio!).toBeGreaterThanOrEqual(4.5);
});

test("侧栏在深色下也是深色背景（不是亮灰）", async () => {
  const r = await win.evaluate(`(() => {
    ${CONTRAST_FN}
    const sb = document.querySelector("[data-testid=shell-sidebar]");
    if (!sb) return null;
    const c = getComputedStyle(sb.firstElementChild || sb).backgroundColor;
    return { c, lum: lum(c) };
  })()`);
  expect(r).not.toBeNull();
  // 深色主题侧栏亮度必须很低
  expect(r!.lum!).toBeLessThan(0.2);
});

test("切到设置页后仍是深色，没有回退到浅色", async () => {
  await win.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim().includes("设置"));
    b?.click();
  });
  await win.waitForTimeout(1200);
  const r = await win.evaluate(`(() => {
    ${CONTRAST_FN}
    const el = document.querySelector("main") || document.body;
    const c = getComputedStyle(el).backgroundColor;
    return { c, lum: lum(c), dark: document.documentElement.classList.contains("dark") };
  })()`);
  expect(r.dark).toBe(true);
  // 设置页背景要么透明（继承），要么是深色
  if (r.c !== "rgba(0, 0, 0, 0)") expect(r.lum!).toBeLessThan(0.3);
});

test("切到自动化页后仍是深色", async () => {
  await win.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim().includes("自动化"));
    b?.click();
  });
  await win.waitForTimeout(1200);
  const r = await win.evaluate(`(() => {
    ${CONTRAST_FN}
    const el = document.querySelector("main") || document.body;
    const c = getComputedStyle(el).backgroundColor;
    return { c, lum: lum(c), dark: document.documentElement.classList.contains("dark") };
  })()`);
  expect(r.dark).toBe(true);
  if (r.c !== "rgba(0, 0, 0, 0)") expect(r.lum!).toBeLessThan(0.3);
});
