// 真实 Electron 的 E2E 跑一层外部看门狗（CI 与本地共用）。
//
// 为什么需要它：playwright.config.ts 里的 `timeout` / `globalTimeout` 都是 **JS 定时器**。
// 实测过两次 Electron 进程挂死或 page.evaluate 卡在不可中断的原生调用上，此时事件循环
// 停摆，定时器自己也不再被调度——用例超时和 globalTimeout 双双失效，步骤一直挂到
// job 级上限才被取消，连带 package / publish 全部 skipped。
//
// 这里把「等待」放到**父进程**上：父进程（本文件所在的 node）不会跟着子进程一起卡死，
// 所以它的 setTimeout 是真能触发的。到点就杀掉子进程树后以 124 退出。
//
// 两道锁的分工：
//   globalTimeout 12min  —— 正常路径的优雅失败（能出报告、能留 trace）
//   本脚本     TIMEOUT   —— 进程僵死时的兜底，保证步骤以非 0 退出、不拖死发布
//
// **杀进程的纪律（血泪教训）**：绝不能按进程名批量杀。ee 桌面端自己就是 Electron，
// 开发时它和 e2e 用的是同一个 electron.exe；`Get-Process electron | Stop-Process`
// 会把开发者正在用的窗口一起干掉（2026-11-04 真的发生过，用户看到窗口闪退）。
// 所以只按两个可区分的东西杀：
//   1. 本脚本拉起的子进程 PID（taskkill /T 连带它的子孙）
//   2. 命令行里带 e2e 专属 userData 标记的进程（e2e/launch.ts 用 mkdtemp 造的
//      `ee-e2e-userdata-*`；正常实例没有 --user-data-dir，天然不会命中）
import { spawn } from "node:child_process";

const TIMEOUT_MS = Number(process.env.E2E_TIMEOUT_MS || 14 * 60_000);
// 与 e2e/launch.ts 的 mkdtemp 前缀一致：这是「只属于本次 e2e」的唯一标记。
const E2E_USERDATA_MARK = "ee-e2e-userdata-";
const isWin = process.platform === "win32";

const child = isWin
  ? spawn(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", "npx", "playwright", "test"],
          { stdio: "inherit", windowsVerbatimArguments: false })
  : spawn("npx", ["playwright", "test"], { stdio: "inherit" });

function killTree(pid) {
  if (isWin) {
    // /T 连带子进程：Electron 和它的 renderer/gpu 都在下面。
    spawn("taskkill", ["/F", "/T", "/PID", String(pid)], { stdio: "ignore" });
  } else {
    try { process.kill(-pid, "SIGKILL"); } catch { try { process.kill(pid, "SIGKILL"); } catch { /* 已退出 */ } }
  }
}

/** 兜底：按 e2e 专属 userData 标记挑出残留的 Electron（不按进程名瞎杀）。 */
function killStrayE2E() {
  if (!isWin) return;
  const ps = [
    "$ErrorActionPreference='SilentlyContinue';",
    `Get-CimInstance Win32_Process -Filter "Name='electron.exe'" |`,
    `Where-Object { $_.CommandLine -and $_.CommandLine.Contains('${E2E_USERDATA_MARK}') } |`,
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
  ].join(" ");
  spawn("powershell", ["-NoProfile", "-NonInteractive", "-Command", ps], { stdio: "ignore" });
}

let timedOut = false;
const timer = setTimeout(() => {
  timedOut = true;
  console.error(`\n[看门狗] E2E 超过 ${Math.round(TIMEOUT_MS / 60000)} 分钟未退出，判定为挂死。`);
  if (child.pid) {
    killTree(child.pid);
    killStrayE2E();
  }
  // 不依赖子进程真的退干净：无论如何在 3 秒后自己退出，避免连看门狗一起挂住。
  setTimeout(() => process.exit(124), 3000).unref();
}, TIMEOUT_MS);
timer.unref?.();

child.on("exit", (code, signal) => {
  clearTimeout(timer);
  if (timedOut) process.exit(124);
  if (signal) {
    console.error(`[看门狗] playwright 被信号 ${signal} 终止`);
    process.exit(1);
  }
  process.exit(code ?? 1);
});

child.on("error", (e) => {
  clearTimeout(timer);
  console.error(`[看门狗] 启动 npx 失败：${e.message}`);
  process.exit(1);
});
