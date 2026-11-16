// 源码开发版桌面端 · **真实模型** 端到端（默认不自启，显式开关才跑）。
//
//   EE_E2E_REAL_MODEL=1 npx playwright test e2e/real-model.spec.ts
//
// 与其它 e2e 的区别：模型是真的（opencode deepseek-v4.1-flash11），会花钱。
// 因此默认 `npm run test:ui` **不会**选到它（模块级 test.skip）。
//
// 隔离与凭据：
// - 经 launchIsolated，EE_HOME 与 EE_TEST_USER_DATA 都是临时目录，用户真实 ~/.ee 与会话不受影响；
// - 临时 home 里只写**非敏感** config.toml（provider/model/family/base_url/budget）；
// - 真实 API key 由后端运行时从系统凭据库解析（config.key_source() 优先查 store），
//   本文件与报告都不含任何密钥。
//
// 覆盖：真实 UI 输入 → 真模型 → 真实工具调用 → 真实判据（target + p2p）→ 交付字段与 UI 文案一致，
// 并核对磁盘真的被改。首轮通过也算桌面闭环通过；不声称自然失败恢复/fanout。

import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { launchIsolated } from "./launch";
import { pageRpc } from "./rpc";

const REPO = path.resolve(__dirname, "..", "..");
const PY = path.join(REPO, ".venv", "Scripts", "python.exe");
const FIXTURE = path.join(REPO, "research", "bench", "allout", "05_slugify_hard");
const EVIDENCE = process.env.EE_E2E_EVIDENCE_DIR || path.join(os.tmpdir(), "ee-e2e-evidence");
const REAL_LOGS = path.join(process.env.APPDATA || "", "ee", "logs");

/** 真实日志目录的文件数 + 最新 mtime（只做前后对比，不读内容）。 */
function logsSnapshot(): { files: number; newest: number } {
  try {
    const names = fs.readdirSync(REAL_LOGS).filter((n) => n.endsWith(".log") || n.includes(".log."));
    const newest = names.reduce((m, n) => Math.max(m, fs.statSync(path.join(REAL_LOGS, n)).mtimeMs), 0);
    return { files: names.length, newest };
  } catch {
    return { files: 0, newest: 0 };
  }
}

const CONFIG_TOML = `default_model = "deepseek-v4.1-flash11"

[budget]
session = 0.30

[permissions]
# 桌面真机默认是「默认模式」（每条命令都要点批准）。本样本测的是闭环而不是审批，
# 用 auto 跑；真机上用户可按自己习惯切模式。
mode = "auto"

[providers.opencode]
base_url = "https://opencode.ai/zen/go/v1"
# opencode 要求每个对话一个稳定 id，缺了直接 400 MissingSessionID；
# 占位符由后端运行时替换，不含任何密钥。
headers = { "x-opencode-session" = "{session_id}" }

[models."deepseek-v4.1-flash11"]
provider = "opencode"
model = "deepseek-v4.1-flash"
family = "deepseek"
`;

const TASK = `实现 slugify.py 的 slugify(text)：小写；常见拉丁扩展字母转写（ä→a ö→o ü→u ß→ss æ→ae ø→o å→a，大小写都算）；
其余非字母数字的连续片段压成一个 -；去掉首尾 -；空串返回空串；长度上限 60，超长时在 ≤60 的最后一个 - 处截断，
没有 - 就硬截 60，截断后再去尾部 -。实现方式自由，只要 tests/ 全过；不要改 tests/ 下的文件。`;

function makeProject(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ee-desk-real-"));
  const proj = path.join(dir, "slugify");
  fs.mkdirSync(path.join(proj, "tests"), { recursive: true });
  fs.copyFileSync(path.join(FIXTURE, "seed", "slugify.py"), path.join(proj, "slugify.py"));
  fs.copyFileSync(path.join(FIXTURE, "judge", "test_judge.py"), path.join(proj, "tests", "test_slugify.py"));
  fs.writeFileSync(path.join(proj, "tests", "test_smoke.py"), "def test_smoke():\n    assert True\n", "utf8");
  fs.writeFileSync(path.join(proj, ".gitignore"), "__pycache__/\n*.pyc\n.pytest_cache/\n", "utf8");
  const run = (...a: string[]) => execFileSync("git", a, { cwd: proj, stdio: "pipe" });
  run("init", "-q", "-b", "main");
  run("config", "user.email", "e2e@example.invalid");
  run("config", "user.name", "e2e");
  run("add", "-A");
  run("commit", "-qm", "seed: slugify is incomplete on purpose");
  return proj;
}

async function openWorkspace(win: Page, proj: string): Promise<string> {
  // 与界面「选择项目文件夹」同一个 RPC（原生目录对话框无法在 Playwright 里点）。
  const r = await pageRpc<{ workspaces: { path: string }[] }>(win, "workspace/open", { path: proj });
  expect(r.workspaces.some((w) => w.path.toLowerCase() === proj.toLowerCase())).toBe(true);
  await win.reload();
  await win.locator("[data-testid=app-shell]").waitFor({ timeout: 60_000 });
  // 左栏点选项目
  await win.getByText(path.basename(proj), { exact: false }).first().click();
  return proj;
}

async function pickModel(win: Page, alias: string): Promise<void> {
  const trigger = win.locator('button[title="模型"]').first();
  await trigger.click();
  // 必须点菜单项（role=menuitem）。点正文里的同名文字只会命中触发器本身，
  // 下拉不会关，随后点「火力全开」会被浮层挡住。
  await win.getByRole("menuitem").filter({ hasText: alias }).first().click();
  await win.keyboard.press("Escape");
  await expect(win.getByRole("menu")).toHaveCount(0, { timeout: 10_000 });
  await expect(trigger).toContainText(alias);
}

async function fillGoal(win: Page, command: string, p2p: string): Promise<void> {
  await win.getByRole("button", { name: "添加", exact: true }).click();
  await win.getByText("目标", { exact: true }).click();
  // placeholder 默认是子串匹配：「例如 pytest -q」也会命中回归命令那条，必须 exact。
  await win.getByPlaceholder("例如 pytest -q", { exact: true }).fill(command);
  await win.getByPlaceholder("回归命令（可选）：例如 pytest -q tests/").fill(p2p);
  await win.getByRole("button", { name: "开始" }).click();
  // 快速失败：带了 goal 才会出现「使用独立判据」。没有就是没发出去，别等 9 分钟。
  await expect(win.getByText("使用独立判据（尚不代表通过）")).toBeVisible({ timeout: 30_000 });
}

async function waitSettled(win: Page, timeoutMs: number): Promise<string> {
  const end = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < end) {
    // 防御：万一仍出现审批卡片（候选继承了非 auto 模式），像用户一样点一次。
    const approve = win.getByRole("button", { name: "允许一次", exact: true }).first();
    if (await approve.count()) {
      await approve.click().catch(() => undefined);
    }
    last = await win.locator("body").innerText();
    if (/交付已复验|交付复验失败|未独立验收|验收依据发生变化|补丁未能写回/.test(last)) return "settled";
    if (/达到花费上限|达到步数上限/.test(last)) return "stopped";
    if (/出错 \d|出错了|连不上模型服务/.test(last)) return "error";
    await win.waitForTimeout(1500);
  }
  return `timeout: ${last.slice(-200)}`;
}

test("源码桌面 · 真模型 · UI→工具→判据→交付 闭环", async () => {
  test.skip(process.env.EE_E2E_REAL_MODEL !== "1",
    "真模型桌面验收默认关闭：设 EE_E2E_REAL_MODEL=1 才跑（会真实扣费）");
  test.setTimeout(11 * 60_000);
  fs.mkdirSync(EVIDENCE, { recursive: true });
  const realLogsBefore = logsSnapshot();
  const proj = makeProject();
  const iso = await launchIsolated({ configToml: CONFIG_TOML, extraEnv: { EE_E2E_REAL_MODEL: "1" } });
  try {
    const win = await iso.app.firstWindow();
    await win.locator("[data-testid=app-shell]").waitFor({ timeout: 60_000 });
    await openWorkspace(win, proj);

    // 真实 UI：选模型 → 开火力全开（2 路上限）→ 填目标与回归命令 → 输入任务
    await pickModel(win, "deepseek-v4.1-flash11");
    await win.getByRole("button", { name: "火力全开 · 关闭" }).click();
    await expect(win.getByRole("button", { name: "火力全开 · 已开启" })).toBeVisible();
    await win.getByLabel("火力全开候选上限").selectOption("2");

    const ta = win.locator("textarea").first();
    await ta.fill(TASK);
    const target = `${PY} -m pytest -q tests`;
    const p2p = `${PY} -m pytest -q tests/test_smoke.py`;
    await fillGoal(win, target, p2p);

    // 隔离契约：日志写到临时 userData/logs，真实日志目录前后不变（只看文件数/mtime，不读内容）。
    const tempLogs = path.join(iso.userData, "logs");
    await expect.poll(() => fs.existsSync(tempLogs) && fs.readdirSync(tempLogs).length > 0,
      { timeout: 60_000, message: "临时 userData/logs 里应有日志" }).toBe(true);
    const realLogsAfter = logsSnapshot();
    expect(realLogsAfter, "测试实例不得写用户真实日志目录").toEqual(realLogsBefore);

    const outcome = await waitSettled(win, 9 * 60_000);
    expect(outcome, `回合没有在 9 分钟内收尾：${outcome}`).toBe("settled");

    // 结构化证据：从后端读会话详情（不读用户会话，只读本次临时 home 里的）
    const threads = await pageRpc<{ threads: { id: string; cwd: string }[] }>(win, "thread/list", {});
    const mine = threads.threads.find((t) => t.cwd.toLowerCase() === proj.toLowerCase());
    expect(mine, "本次临时项目应有一条会话").toBeTruthy();
    // thread/get 返回 {thread: {...}}（与 store 的用法一致）
    const detail = (await pageRpc<{ thread: any }>(
      win, "thread/get", { thread_id: mine!.id }, 60_000)).thread;
    const turnEnd = [...detail.items].reverse().find((it) => it.type === "turn_end");
    expect(turnEnd, "回合末尾应有 turn_end").toBeTruthy();
    const delivery = turnEnd.delivery || {};
    const tools = detail.items.filter((it) => it.type === "tool");

    // 1) 真实工具调用（不是假模型）
    expect(tools.some((t) => t.status === "ok"), "应至少有成功的真实工具调用").toBe(true);
    // 2) 真实判据通过（target + p2p）
    expect(delivery.status, `交付状态应为 verified，实际 ${delivery.status}`).toBe("verified");
    expect(delivery.target?.met).toBe(true);
    expect(delivery.target?.reason || "", "target 结论应来自真实判据").toContain("测试通过");
    expect(delivery.regression?.status).toBe("met");
    // 3) UI 文案与 delivery 一致
    const body = await win.locator("body").innerText();
    expect(body).toContain("交付已复验");
    // 4) 计划/实际口径分开：面板写「请求 N 路 → 本轮 M 路」，不能把计划值说成实际；
    //    没有真的启动多路时也不应出现「实际启动」（我们已把文案改成「计划启动」）。
    const planText = /请求\s*(\d+)\s*路\s*→\s*本轮\s*(\d+)\s*路/.exec(body);
    expect(planText, `面板应显示计划路数，实际文本片段：${body.slice(-400)}`).toBeTruthy();
    expect(Number(planText![2])).toBeGreaterThanOrEqual(1);
    expect(body, "计划值不能冒充实际启动").not.toContain("实际启动");
    if (detail.allout_plan) expect(detail.allout_plan.candidates).toBeGreaterThanOrEqual(1);
    // 5) 磁盘真的被改（不是只回显）
    const code = fs.readFileSync(path.join(proj, "slugify.py"), "utf8");
    expect(code.length).toBeGreaterThan(120);
    expect(code).not.toContain('"-".join(text.strip().lower().split())');
    const status = execFileSync("git", ["status", "--porcelain"], { cwd: proj }).toString();
    expect(status).toContain("slugify.py");

    await win.screenshot({ path: path.join(EVIDENCE, "real-model-desktop.png"), fullPage: false });
    const cost = turnEnd.usage?.cost;
    fs.writeFileSync(path.join(EVIDENCE, "real-model-desktop.json"), JSON.stringify({
      delivery: { status: delivery.status, target: delivery.target?.reason,
                  regression: delivery.regression?.status, source: delivery.source },
      plan: detail.allout_plan || null, allout_started: detail.allout_started ?? null,
      tools: tools.map((t) => ({ name: t.name, status: t.status })),
      usage: { cost: cost ?? null, requests: turnEnd.usage?.requests ?? null },
      model: turnEnd.model || null,
    }, null, 2), "utf8");
  } finally {
    await iso.dispose();
    fs.rmSync(path.dirname(proj), { recursive: true, force: true });
  }
});
