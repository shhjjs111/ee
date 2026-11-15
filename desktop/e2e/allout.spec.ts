import { expect, test } from "@playwright/test";
import { launchIsolated } from "./launch";

test("火力全开入口、上限选择和窄窗口布局", async () => {
  const iso = await launchIsolated();
  try {
    const win = await iso.app.firstWindow();
    await win.locator("[data-testid=app-shell]").waitFor({ timeout: 60000 });
    const toggle = win.getByRole("button", { name: "火力全开 · 关闭" });
    await toggle.click();
    await expect(win.getByRole("button", { name: "火力全开 · 已开启" })).toHaveAttribute("aria-pressed", "true");
    const cap = win.getByLabel("火力全开候选上限");
    await cap.selectOption("6");
    await expect(cap).toHaveValue("6");
    const parallel = win.getByRole("checkbox", { name: "本轮测试相互隔离，可并行验收" });
    await expect(parallel).not.toBeChecked();
    await parallel.check();
    await iso.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(960, 640));
    await expect(cap).toBeVisible();
    expect(await win.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await win.getByRole("button", { name: "火力全开 · 已开启" }).click();
    await expect(cap).toHaveCount(0);
  } finally {
    await iso.dispose();
  }
});
