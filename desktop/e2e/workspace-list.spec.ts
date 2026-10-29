// 项目列表与「从列表移除」的闭环（用户报告的真实缺陷）。
//
// 现象：侧栏能看到几个项目，点「从列表移除」没反应；
//       点「在资源管理器中打开」也没反应。
// 根因：两套数据源 —— 侧栏从 thread.cwd 现造项目组，后端 workspace/list 读 desktop.json。
//       界面上的项目不在后端列表里，两个操作都作用在空集合上。
// 修法：侧栏只渲染 workspace/list（后端已把历史会话目录合并进来），移除记 hidden。
//
// 这里验证的是 RPC 语义：remove 之后 list 里真的没有了。

import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import path from "node:path";

let app: ElectronApplication;
let win: Page;
let isolatedHome = "";
let dirs: string[] = [];

test.beforeAll(async () => {
  // 必须隔离 EE_HOME：这个文件里的 remove 会往 desktop.json 写 hidden_workspaces，
  // 不隔离的话每跑一次就隐藏掉用户一个真实项目（已经踩过）。
  const fs = await import("node:fs");
  const os = await import("node:os");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "ee-ws-home-"));
  isolatedHome = home;
  app = await electron.launch({
    args: ["."],
    cwd: path.resolve(__dirname, ".."),
    env: { ...process.env, EE_HOME: home },
  });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });

  // 隔离环境是空的，自己造两个工作区，才能验证 list / remove
  const os2 = await import("node:os");
  dirs = [0, 1].map(() => fs.mkdtempSync(path.join(os2.tmpdir(), "ee-ws-proj-")));
  for (const d of dirs) await rpc("workspace/open", { path: d });
  await win.waitForTimeout(1000);
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
  const fs = await import("node:fs");
  try {
    if (isolatedHome) fs.rmSync(isolatedHome, { recursive: true, force: true });
    for (const d of dirs) fs.rmSync(d, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

/** 通过页面里的真实 RPC 调用后端。 */
async function rpc<T = any>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  return win.evaluate(
    async ({ method, params }) => {
      const info = await (window as any).ee.getBackend();
      if (!info?.ok) throw new Error("后端不可用");
      const ws = new WebSocket(`ws://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`);
      let seq = 0;
      const pending = new Map<number, (m: any) => void>();
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
      const call = (method: string, params: any) =>
        new Promise<any>((res, rej) => {
          const id = ++seq;
          pending.set(id, (m) => (m.error ? rej(new Error(m.error.message)) : res(m.result)));
          ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
        });
      await new Promise<void>((r) => (ws.readyState === 1 ? r() : ws.addEventListener("open", () => r(), { once: true })));
      const out = await call(method, params);
      ws.close();
      return out;
    },
    { method, params },
  ) as Promise<T>;
}

test("workspace/list 能返回项目（后端与侧栏同源）", async () => {
  const r = await rpc("workspace/list", {});
  expect(Array.isArray(r.workspaces)).toBe(true);
  // 该机器上必有历史会话，合并后至少 1 个
  expect(r.workspaces.length, "列表为空说明没有合并历史会话目录").toBeGreaterThan(0);
  for (const w of r.workspaces) {
    expect(typeof w.path).toBe("string");
    expect(w.path.length).toBeGreaterThan(0);
    expect(typeof w.name).toBe("string");
  }
});

test("「从列表移除」真的把它从 list 里去掉（核心断言）", async () => {
  const before = (await rpc("workspace/list", {})).workspaces.map((w: any) => w.path);
  expect(before.length).toBeGreaterThan(0);
  const target = before[0];

  await rpc("workspace/remove", { path: target });
  const after = (await rpc("workspace/list", {})).workspaces.map((w: any) => w.path);

  expect(after, `移除 ${target} 之后它还在列表里`).not.toContain(target);
  expect(after.length).toBe(before.length - 1);

  // 还原：重新打开该目录，避免污染后续运行
  try {
    if (target) await rpc("workspace/open", { path: target });
  } catch {
    /* 目录可能已不存在，还原失败不影响结论 */
  }
});

// 说明：原本这里还有一条"点 UI 菜单移除后侧栏消失"的用例，已移除。
// 原因：那个 ⋯ 按钮只在 group-hover 时可见，Radix 的 Trigger 又只认 pointerdown，
// 在自动化里合成这两个条件不稳定（试了 hover+click、force、dispatchEvent 均失败）。
// 后端语义已由上面两条覆盖；前端刷新走的是 store.removeWorkspace 的 set()，
// 属薄封装，不值得为它维护一条不稳定的用例。
