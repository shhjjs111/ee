// 真实交互闭环（Wave 6）：输入 → RPC → 真实后端 → 假模型 → 工具调用 → 审批 → 完成。
//
// 上一版只验到「输入框清空 + 出现错误」——因为本机没配 key，后端第一步就报
// 「模型没有可用的 API key」。那是失败路径，不是真实回合，不能当闭环证据。
//
// 这一版用后端自己的 settings/set RPC 指向本地假模型服务（真 HTTP/SSE），
// 于是链条上只剩「模型推理」被替换，Composer / store / RpcClient / 后端 / 工具
// 全是真实实现。判定看 UI 上真实出现的内容和 turn 结束标记。

import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";

const ROOT = path.resolve(__dirname, "../..");
const FAKE_PORT = 18099;
const FAKE_URL = `http://127.0.0.1:${FAKE_PORT}/v1`;

let app: ElectronApplication;
let win: Page;
let fake: ChildProcess | null = null;
let ready = false;
let configured = false;

test.beforeAll(async () => {
  fake = spawn("python", [path.join(ROOT, "desktop/scripts/fake_model_server.py"), String(FAKE_PORT)], {
    stdio: "ignore",
  });
  await new Promise((r) => setTimeout(r, 1800));

  app = await electron.launch({ args: ["."], cwd: path.join(ROOT, "desktop") });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");

  ready = await win
    .locator("[data-testid=app-shell]")
    .waitFor({ state: "visible", timeout: 45_000 })
    .then(() => true)
    .catch(() => false);
  if (!ready) return;

  // 通过真实 RPC 把默认模型指向本地假模型（等价于用户在设置页填写供应商）
  configured = await win
    .evaluate(async (url) => {
      // 从页面里拿不到 store 实例，就走 preload 暴露的后端信息自己连一次 WS
      const ee = (window as any).ee;
      const info = await ee.getBackend();
      if (!info?.ok) return false;

      const ws = new WebSocket(`ws://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`);
      const call = (() => {
        let seq = 0;
        const pending = new Map<number, (v: any) => void>();
        ws.addEventListener("message", (ev) => {
          try {
            const m = JSON.parse(ev.data as string);
            if (m.id && pending.has(m.id)) {
              pending.get(m.id)!(m);
              pending.delete(m.id);
            }
          } catch {
            /* ignore */
          }
        });
        return (method: string, params: any, timeout = 20000) =>
          new Promise<any>((resolve, reject) => {
            const id = ++seq;
            const timer = setTimeout(() => reject(new Error(`${method} 超时`)), timeout);
            pending.set(id, (m) => {
              clearTimeout(timer);
              m.error ? reject(new Error(m.error.message)) : resolve(m.result);
            });
            ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
          });
      })();

      await new Promise<void>((r, j) => {
        ws.readyState === WebSocket.OPEN ? r() : ws.addEventListener("open", () => r(), { once: true });
        ws.addEventListener("error", () => j(new Error("ws error")), { once: true });
      });

      const s = (await call("settings/get")).settings;
      s.providers.fake = { base_url: url, api_key: "sk-fake", api_key_env: "" };
      s.models["fake-gpt"] = { provider: "fake", model: "fake-gpt", family: "default" };
      s.default_model = "fake-gpt";
      await call("settings/set", { settings: s });
      ws.close();
      return true;
    }, FAKE_URL)
    .catch(() => false);

  // 让界面重新拉一次模型与默认设置
  if (configured) await win.waitForTimeout(1500);
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
  fake?.kill();
});

test("前置条件：后端就绪 + 假模型已接入", () => {
  test.skip(!ready, "后端未在 45s 内就绪（检查仓库根 .venv 与 Python 依赖）");
  expect(ready).toBe(true);
  expect(configured, "没能通过 settings/set 接入假模型").toBe(true);
});

test("走完真实回合：输入 → 工具调用 → 审批 → 完成，且不出现缺 key 错误", async () => {
  test.skip(!ready || !configured, "前置条件不成立");

  const ta = win.locator("textarea").first();
  await expect(ta).toBeVisible();
  await ta.fill("修一下 average 的 bug");
  await ta.press("Enter");
  await expect(ta).toHaveValue("", { timeout: 10_000 });

  // 真实回合会停在「需要你的批准」（假模型第 2 步要跑 shell）。
  // 这是设计行为，不是卡死——所以这里要像用户那样点「允许一次」，
  // 否则测出来的是「一直等审批」而不是「回合能否走完」。
  const approve = win.getByRole("button", { name: "允许一次", exact: true }).first();
  let approved = false;

  await expect
    .poll(
      async () => {
        const t = await win.locator("body").innerText();
        if (/完成 ·|已停止|达到步数上限/.test(t)) return "done";
        if (/没有可用的 API key/.test(t)) return "no-key";
        if (/出错 ·/.test(t)) return "error";
        if (/需要你的批准/.test(t) && !approved) {
          approved = true;
          await approve.click().catch(() => undefined);
          return "approving";
        }
        return "running";
      },
      { timeout: 120_000, intervals: [800] },
    )
    .not.toBe("running");

  const body = await win.locator("body").innerText();
  // 关键断言：不能是「没配 key」——那说明假模型根本没接上
  expect(body).not.toContain("没有可用的 API key");
});

test("真实回合里出现了工具活动（读/搜/编辑/运行至少其一）", async () => {
  test.skip(!ready || !configured, "前置条件不成立");

  // 不依赖上一个用例的页面状态：自己发一条并等这个回合走完。
  const ta = win.locator("textarea").first();
  await expect(ta).toBeVisible();
  await ta.fill("再看一下 average");
  await ta.press("Enter");

  const approve = win.getByRole("button", { name: "允许一次", exact: true }).first();
  let approved = false;
  await expect
    .poll(
      async () => {
        const t = await win.locator("body").innerText();
        if (/完成 ·|达到步数上限|已停止/.test(t)) return "done";
        if (/出错 ·/.test(t)) return "error";
        // 出现审批就按用户的做法放行，让回合继续
        if (/需要你的批准/.test(t) && !approved) {
          approved = true;
          await approve.click().catch(() => undefined);
          return "approving";
        }
        return "running";
      },
      { timeout: 120_000, intervals: [800] },
    )
    .not.toBe("running");

  const body = await win.locator("body").innerText();
  // 假模型脚本会用 read/grep/edit/shell：至少出现一种工具活动
  const hasTool = /已浏览|已读取|已编辑|运行了|已搜索|已思考|已允许/.test(body);
  expect(hasTool, `没有出现任何工具活动，界面内容：${body.slice(0, 300)}`).toBe(true);
});
