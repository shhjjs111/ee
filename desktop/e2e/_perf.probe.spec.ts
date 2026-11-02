// 临时性能探针（测完删除）：构造「另一个会话正在长时间流式输出」的真实场景，
// 量「切到大会话」的耗时、长任务与帧间隔，作为优化前后的对照。
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

let app: ElectronApplication;
let win: Page;
let home = "";
let proj = "";
let userData = "";
let server: http.Server | null = null;
const port = 18123;
const BIG = "20260101-000000-aaaaaa";
const SMALL = "20260101-000001-bbbbbb";

function item(id: string, seq: number, body: Record<string, unknown>) {
  return JSON.stringify({ t: 1, kind: "item", item: { id, seq, ts: 1, ...body } });
}

function makeSession(id: string, turns: number, title: string) {
  const lines: string[] = [];
  lines.push(JSON.stringify({ t: 1, kind: "meta", meta: { cwd: proj, title, created: 1, updated: 2, model: "m", mode: "default" } }));
  let seq = 1;
  for (let turn = 0; turn < turns; turn++) {
    lines.push(item(`us_${turn}`, seq++, { type: "user", text: `第 ${turn + 1} 个问题：这里是一段比较长的用户提问。`.repeat(3) }));
    for (let k = 0; k < 6; k++) {
      lines.push(item(`to_${turn}_${k}`, seq++, {
        type: "tool", call_id: `c${turn}_${k}`, name: "shell", args: { command: "pytest -q" }, summary: "跑测试",
        status: "ok", output: ("测试输出行，包含一些路径和数字。\n").repeat(40), details: { exit: 0 },
      }));
      lines.push(item(`as_${turn}_${k}`, seq++, {
        type: "assistant", streaming: false,
        text: ("## 小节\n\n这是一段回复，包含 `行内代码` 与列表：\n\n- 要点一\n- 要点二\n\n```python\ndef f(x):\n    return x + 1\n```\n\n").repeat(6),
        reasoning: "",
      }));
    }
    lines.push(item(`te_${turn}`, seq++, { type: "turn_end", reason: "completed", duration_ms: 1000, usage: { request_count: 1 } }));
  }
  fs.writeFileSync(path.join(home, "sessions", `${id}.jsonl`), lines.join("\n") + "\n", "utf8");
}

function startStreamingServer(): Promise<void> {
  server = http.createServer((req, res) => {
    if (req.method !== "POST") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ data: [] }));
      return;
    }
    req.resume();
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const words = ("慢速流式输出用于模拟长会话持续推送。").split("");
    let i = 0;
    const timer = setInterval(() => {
      // 一直流：循环输出，直到客户端断开（模拟「跑了很久的会话」）
      res.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta: { content: words[i++ % words.length] } }] })}\n\n`);
    }, 20);
    req.on("close", () => clearInterval(timer));
  });
  return new Promise((r) => server!.listen(port, "127.0.0.1", () => r()));
}

async function rpc(method: string, params: Record<string, unknown>) {
  return win.evaluate(async ({ method, params }) => {
    const info = await (window as any).ee.getBackend();
    const ws = new WebSocket(`ws://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`);
    await new Promise<void>((r, j) => {
      ws.readyState === WebSocket.OPEN ? r() : ws.addEventListener("open", () => r(), { once: true });
      ws.addEventListener("error", () => j(new Error("ws error")), { once: true });
    });
    const result = await new Promise<any>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`${method} 超时`)), 60000);
      ws.addEventListener("message", (ev) => {
        const m = JSON.parse(ev.data as string);
        if (!m.id) return;
        clearTimeout(timer);
        m.error ? reject(new Error(m.error.message)) : resolve(m.result);
      });
      ws.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }));
    });
    ws.close();
    return result;
  }, { method, params });
}

/** 纯 RPC 往返耗时（不含渲染）：后端序列化 + WebSocket + 渲染进程 JSON.parse。 */
async function rpcTiming(threadId: string) {
  return win.evaluate(async (threadId) => {
    const info = await (window as any).ee.getBackend();
    const ws = new WebSocket(`ws://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`);
    await new Promise<void>((r, j) => {
      ws.readyState === WebSocket.OPEN ? r() : ws.addEventListener("open", () => r(), { once: true });
      ws.addEventListener("error", () => j(new Error("ws error")), { once: true });
    });
    const t0 = performance.now();
    let bytes = 0;
    const result = await new Promise<any>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("超时")), 60000);
      ws.addEventListener("message", (ev) => {
        const raw = ev.data as string;
        bytes = raw.length;
        // 用 performance.now 包住「收到字符串 → JSON.parse 完成」
        const m = JSON.parse(raw);
        clearTimeout(timer);
        m.error ? reject(new Error(m.error.message)) : resolve(m.result);
      });
      ws.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "thread/get", params: { thread_id: threadId } }));
    });
    const ms = performance.now() - t0;
    ws.close();
    return { ms, bytes, items: result.thread?.items?.length ?? 0 };
  }, threadId);
}

const measureFrames = (ms: number) =>
  win.evaluate(async (ms) => {
    const gaps: number[] = [];
    await new Promise<void>((resolve) => {
      let last = performance.now();
      const end = last + ms;
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
    return {
      frames: gaps.length,
      avg: Math.round((gaps.reduce((a, b) => a + b, 0) / Math.max(1, gaps.length)) * 10) / 10,
      p95: Math.round((sorted[Math.floor(sorted.length * 0.95)] ?? 0) * 10) / 10,
      worst: Math.round((sorted[sorted.length - 1] ?? 0) * 10) / 10,
    };
  }, ms);

async function openByTitle(title: string) {
  const row = win.locator(`[data-testid=thread-row][data-title="${title}"]`).first();
  await expect(row).toBeVisible({ timeout: 20000 });
  const t0 = Date.now();
  await row.click();
  await win.waitForFunction(
    (t) => [...document.querySelectorAll("[data-testid=thread-row]")].some((el) => (el as HTMLElement).getAttribute("data-title") === t && el.className.includes("bg-active")),
    title, { timeout: 40000 },
  );
  await win.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
  return Date.now() - t0;
}

const startLongTaskObserver = () =>
  win.evaluate(() => {
    (window as any).__long = [];
    (window as any).__obs?.disconnect?.();
    (window as any).__obs = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) (window as any).__long.push(Math.round(e.duration));
    });
    (window as any).__obs.observe({ entryTypes: ["longtask"] });
  });

const readLongTasks = () => win.evaluate(() => {
  const arr = (window as any).__long as number[];
  const sorted = [...arr].sort((a, b) => b - a);
  return { count: arr.length, total: arr.reduce((a, b) => a + b, 0), top: sorted.slice(0, 8) };
});

test.beforeAll(async () => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), "ee-perf-home-"));
  proj = fs.mkdtempSync(path.join(os.tmpdir(), "ee-perf-proj-"));
  userData = fs.mkdtempSync(path.join(os.tmpdir(), "ee-perf-userdata-"));
  fs.mkdirSync(path.join(home, "sessions"), { recursive: true });
  fs.writeFileSync(path.join(home, "desktop.json"), JSON.stringify({ workspaces: [{ path: proj, name: "perf-proj", opened: 1 }] }));
  makeSession(BIG, 80, "大会话（性能样本）");
  makeSession(SMALL, 2, "小会话");
  await startStreamingServer();
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
  server?.close();
  for (const d of [home, proj, userData]) {
    try {
      fs.rmSync(d, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
});

test("1. 冷启打开大会话：RPC / 长任务 / 帧间隔", async () => {
  const rpcT = await rpcTiming(BIG);
  await startLongTaskObserver();
  const openMs = await openByTitle("大会话（性能样本）");
  const blocks = await win.evaluate(() => {
    const inner = document.querySelector(".overflow-y-auto > div");
    return {
      blocks: document.querySelectorAll("[data-block]").length,
      rows: inner ? inner.children.length : 0,
      domNodes: document.querySelectorAll(".overflow-y-auto *").length,
    };
  });
  const long = await readLongTasks();
  await win.waitForTimeout(300);
  const frames = await measureFrames(1500);
  console.log("PERF cold rpc=", JSON.stringify(rpcT), "open=", openMs, "blocks=", blocks, "long=", JSON.stringify(long), "frames=", JSON.stringify(frames));
});

test("2. 后台流式时切会话：帧间隔", async () => {
  const s = (await rpc("settings/get", {})).settings;
  s.providers.slow = { base_url: `http://127.0.0.1:${port}/v1`, api_key: "sk-x", api_key_env: "" };
  s.models["slow-model"] = { provider: "slow", model: "slow-model", family: "default" };
  s.default_model = "slow-model";
  await rpc("settings/set", { settings: s });

  const started = await rpc("thread/start", { cwd: proj, model: "slow-model" });
  const tid = started.thread.id as string;
  void rpc("turn/start", { thread_id: tid, text: "一直输出", model: "slow-model" }).catch(() => undefined);
  await win.waitForTimeout(2500);

  await startLongTaskObserver();
  const toSmall = await openByTitle("小会话");
  const framesSmall = await measureFrames(2500);
  const longSmall = await readLongTasks();

  await startLongTaskObserver();
  const toBig = await openByTitle("大会话（性能样本）");
  const framesBig = await measureFrames(2500);
  const longBig = await readLongTasks();
  console.log("PERF streaming toSmall=", toSmall, "framesSmall=", JSON.stringify(framesSmall), "longSmall=", JSON.stringify(longSmall));
  console.log("PERF streaming toBig=", toBig, "framesBig=", JSON.stringify(framesBig), "longBig=", JSON.stringify(longBig));
});

test("3. 活动会话流式刷新时的帧间隔", async () => {
  // 当前停在大会话；用同一会话再起一个流式回合
  const s = (await rpc("settings/get", {})).settings;
  s.default_model = "slow-model";
  await rpc("settings/set", { settings: s });
  await startLongTaskObserver();
  void rpc("turn/start", { thread_id: BIG, text: "继续输出", model: "slow-model" }).catch(() => undefined);
  await win.waitForTimeout(1200);
  const frames = await measureFrames(2500);
  const long = await readLongTasks();
  console.log("PERF activeStream frames=", JSON.stringify(frames), "long=", JSON.stringify(long));
});
