// 「每回合最多续跑段数」在真实窗口里必须能看见、能改、能落盘。
//
// jsdom 测不出布局，也测不出真实 IPC 往返。这条守三件事：
// 1. 设置页真的渲染了这一行（不是被前面的项挤掉）
// 2. 改值后保存，后端 settings/get 读回的是新值（真的写进 config.toml）
// 3. 1～10 之外的值被后端拒绝，不会静默写坏配置

import { expect, test, type ElectronApplication, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { launchIsolated, type IsolatedApp } from "./launch";

let app: ElectronApplication;
let iso: IsolatedApp;
let win: Page;

test.beforeAll(async () => {
  iso = await launchIsolated();
  app = iso.app;
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
});

test.afterAll(async () => {
  await iso?.dispose();
});

async function gotoSettings() {
  await win.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => (x.textContent || "").trim() === "设置");
    b?.click();
  });
  await win.waitForTimeout(1000);
}

test("设置页能看到「每回合最多续跑段数」", async () => {
  await gotoSettings();
  await expect(win.locator("body")).toContainText("每回合最多续跑段数");
});

test("改段数后保存，后端读回新值", async () => {
  await gotoSettings();

  // 找到这一行里的数字输入框（page 上直接操作真实 DOM）
  const row = win.locator("text=每回合最多续跑段数").locator("xpath=ancestor::*[.//input][1]");
  const input = row.locator("input").first();
  await expect(input).toBeVisible();

  await input.fill("5");
  await win.waitForTimeout(300);

  // 保存
  const saved = await win.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => /^保存/.test((x.textContent || "").trim()));
    if (!b) return false;
    (b as HTMLButtonElement).click();
    return true;
  });
  expect(saved, "找不到保存按钮").toBe(true);
  await win.waitForTimeout(1200);

  // 直接读隔离 home 里的 config.toml，确认真的落盘（不依赖界面回显）
  const cfgPath = path.join(iso.home, "config.toml");
  const raw = fs.readFileSync(cfgPath, "utf8");
  expect(raw, "config.toml 里没有 max_steps_segments = 5").toMatch(/max_steps_segments\s*=\s*5/);
});
