// 临时真机验证（验完删除）：用**真实**的 15MB 大会话，检查窗口化后的
// 打开耗时、滚动到底、往上翻「显示更早」时的滚动位置保持。
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const REAL = path.join(os.homedir(), ".ee", "sessions", "20261101-201905-43ca2f.jsonl");
const REAL_ID = "20261101-201905-43ca2f";


let app: ElectronApplication;
let win: Page;
let home = "";
let userData = "";

test.beforeAll(async () => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), "ee-real-home-"));
  userData = fs.mkdtempSync(path.join(os.tmpdir(), "ee-real-userdata-"));
  fs.mkdirSync(path.join(home, "sessions"), { recursive: true });
  fs.copyFileSync(REAL, path.join(home, "sessions", `${REAL_ID}.jsonl`));
  fs.writeFileSync(
    path.join(home, "desktop.json"),
    JSON.stringify({ workspaces: [{ path: "E:\\ee建设\\ee-main", name: "ee", opened: 1 }] }),
  );
  app = await electron.launch({
    args: ["."],
    cwd: path.resolve(__dirname, ".."),
    env: { ...process.env, EE_HOME: home, EE_TEST_USER_DATA: userData },
  });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60000 });
  await win.waitForTimeout(800);
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
  for (const d of [home, userData]) {
    try {
      fs.rmSync(d, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
});


test("真实大会话：thread/get 纯 RPC 耗时（含后端加载首帧）", async () => {
  const r = await win.evaluate(async (threadId) => {
    const info = await (window as any).ee.getBackend();
    const ws = new WebSocket(`ws://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`);
    await new Promise<void>((res, j) => {
      ws.readyState === WebSocket.OPEN ? res() : ws.addEventListener("open", () => res(), { once: true });
      ws.addEventListener("error", () => j(new Error("ws error")), { once: true });
    });
    const call = (id: number) =>
      new Promise<any>((resolve, reject) => {
        const t0 = performance.now();
        const h = (ev: MessageEvent) => {
          const raw = ev.data as string;
          const m = JSON.parse(raw);
          if (m.id !== id) return;
          ws.removeEventListener("message", h);
          resolve({ ms: Math.round(performance.now() - t0), bytes: raw.length, parsed: m.result });
        };
        ws.send(JSON.stringify({ jsonrpc: "2.0", id, method: "thread/get", params: { thread_id: threadId } }));
        setTimeout(() => reject(new Error("timeout")), 60000);
      });
    const cold = await call(1);
    const warm = await call(2);
    ws.close();
    return { cold: cold.ms, warm: warm.ms, bytes: cold.bytes, items: cold.parsed.thread.items.length };
  }, REAL_ID);
  console.log("REAL rpc=", JSON.stringify(r));
});

test("真实 15MB 大会话：窗口化打开 + 滚动 + 加载更早", async () => {
  const row = win.locator(`[data-testid=thread-row]`).first();
  await expect(row).toBeVisible({ timeout: 20000 });
  const before = await win.evaluate(() => performance.now());
  await row.click();
  // 等首屏内容渲染（有消息文本）
  await win.waitForFunction(
    () => (() => { const all = [...document.querySelectorAll(".overflow-y-auto")]; const m = all.sort((a,b)=>b.clientHeight-a.clientHeight)[0]; return (m?.textContent?.length ?? 0) > 500; })(),
    undefined,
    { timeout: 60000 },
  );
  await win.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
  const openMs = await win.evaluate((t0) => Math.round(performance.now() - t0), before);

  const dom = await win.evaluate(() => {
    const el = ([...document.querySelectorAll(".overflow-y-auto")].sort((a, b) => b.clientHeight - a.clientHeight)[0]) as HTMLElement;
    const inner = el?.firstElementChild as HTMLElement;
    return {
      blocks: inner ? inner.children.length : 0,
      domNodes: el ? el.querySelectorAll("*").length : 0,
      scrollTop: Math.round(el?.scrollTop ?? 0),
      scrollHeight: Math.round(el?.scrollHeight ?? 0),
      clientHeight: Math.round(el?.clientHeight ?? 0),
      atBottom: el ? el.scrollHeight - el.scrollTop - el.clientHeight < 80 : false,
      loadEarlier: !!document.querySelector("[data-testid=load-earlier]"),
    };
  });
  console.log("REAL open=", openMs, "dom=", JSON.stringify(dom));

  // 自动跟随：打开时应停在底部
  expect(dom.atBottom, `打开后应停在底部：${JSON.stringify(dom)}`).toBe(true);

  // 往上滚：滚到顶部附近应自动补一批，且视觉位置不跳
  const beforeClick = await win.evaluate(() => {
    const el = ([...document.querySelectorAll(".overflow-y-auto")].sort((a, b) => b.clientHeight - a.clientHeight)[0]) as HTMLElement;
    el.scrollTop = 0;
    return { h: el.scrollHeight, count: (el.firstElementChild as HTMLElement).children.length };
  });
  await win.waitForTimeout(600);
  const afterClick = await win.evaluate(() => {
    const el = ([...document.querySelectorAll(".overflow-y-auto")].sort((a, b) => b.clientHeight - a.clientHeight)[0]) as HTMLElement;
    return {
      h: el.scrollHeight,
      top: Math.round(el.scrollTop),
      count: (el.firstElementChild as HTMLElement).children.length,
      loadEarlier: !!document.querySelector("[data-testid=load-earlier]"),
    };
  });
  console.log("REAL earlier before=", JSON.stringify(beforeClick), "after=", JSON.stringify(afterClick));
  expect(afterClick.count, "应追加了更早的区块").toBeGreaterThan(beforeClick.count);

  // 截图：滚回底部看真实渲染
  await win.evaluate(() => {
    const el = ([...document.querySelectorAll(".overflow-y-auto")].sort((a, b) => b.clientHeight - a.clientHeight)[0]) as HTMLElement;
    el.scrollTop = el.scrollHeight;
  });
  await win.waitForTimeout(400);
  await win.screenshot({ path: path.resolve(__dirname, "..", "test-results", "_real-big.png") });

  // 帧间隔：滚完这一段之后是否还流畅
  const frames = await win.evaluate(async () => {
    const gaps: number[] = [];
    await new Promise<void>((resolve) => {
      let last = performance.now();
      const end = last + 1500;
      const tick = (t: number) => {
        gaps.push(t - last);
        last = t;
        if (t < end) requestAnimationFrame(tick);
        else resolve();
      };
      requestAnimationFrame(tick);
    });
    gaps.shift();
    const sorted = [...gaps].sort((a, b) => a - b);
    return { frames: gaps.length, avg: Math.round((gaps.reduce((a, b) => a + b, 0) / gaps.length) * 10) / 10, p95: Math.round(sorted[Math.floor(sorted.length * 0.95)] * 10) / 10 };
  });
  console.log("REAL frames=", JSON.stringify(frames));
});
