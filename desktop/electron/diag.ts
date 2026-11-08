// 卡死诊断（P0-3）：界面无响应时导出「到底是卡在哪一层」的快照。
//
// 为什么要有这个：实测一次「输入框打不进字」只能靠重启系统恢复，事后没有任何现场证据
// ——主进程是否在转、渲染进程的 JS 线程是否被卡住、哪个子进程在吃 CPU，全都无从判断。
//
// 关键设计：**探针从主进程发起，不依赖渲染进程的 JS 有反应。**
// - 用 executeJavaScript 跑一段 100ms 定时器，量它的实际延迟：JS 线程被卡住时它永远不返回；
// - 每项探测都有超时，渲染进程完全不响应时快照照样能写出来（这本身就是结论）；
// - 只在托盘菜单里触发：界面卡死时设置页点不到，托盘菜单始终可点。
import { app, BrowserWindow, ipcMain } from "electron";
import fs from "node:fs";
import path from "node:path";

export type DiagResult = { ok: boolean; file?: string; summary: string };

/** 在渲染进程里跑一段脚本并限时等结果；超时就返回 null（渲染进程那一刻不可用）。 */
async function probe(win: BrowserWindow, script: string, timeoutMs: number): Promise<unknown> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  try {
    const timeout = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), timeoutMs);
    });
    const run = win.webContents.executeJavaScript(script, true).catch(() => null);
    const r = await Promise.race([run, timeout]);
    return r ?? null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** 量渲染进程事件循环的延迟：定时器 100ms，实际过了多久才回调。JS 线程被打满时这个值会暴涨或永远不回来。 */
const LAG_SCRIPT = `new Promise((resolve) => {
  const t0 = performance.now();
  setTimeout(() => {
    const lag = performance.now() - t0 - 100;
    resolve({
      lag_ms: Math.round(lag),
      has_focus: document.hasFocus(),
      visibility: document.visibilityState,
      active: (document.activeElement && (document.activeElement.tagName || "")) || "",
      active_editable: !!(document.activeElement && (
        document.activeElement.tagName === "TEXTAREA" || document.activeElement.tagName === "INPUT")),
      node_count: document.getElementsByTagName("*").length,
    });
  }, 100);
})`;

export function registerDiagIpc(opts: { getWin: () => BrowserWindow | null; logsDir: string }) {
  const collect = async (): Promise<DiagResult> => {
    const win = opts.getWin();
    const now = new Date();
    const facts: Record<string, unknown> = {
      ts: now.toISOString(),
      app: { version: app.getVersion(), packaged: app.isPackaged, locale: app.getLocale() },
      platform: { os: process.platform, arch: process.arch, versions: process.versions, uptime_s: Math.round(process.uptime()) },
      main: { pid: process.pid, memory_rss: process.memoryUsage().rss },
      window: null as unknown,
      renderer: null as unknown,
      metrics: [] as unknown[],
    };

    if (!win || win.isDestroyed()) {
      facts.window = { exists: false };
    } else {
      const wc = win.webContents;
      facts.window = {
        exists: true,
        visible: win.isVisible(),
        minimized: win.isMinimized(),
        focused: win.isFocused(),
        destroyed: wc.isDestroyed(),
        loading: wc.isLoading(),
        crashed: wc.isCrashed(),
        pid: wc.getOSProcessId(),
        bounds: win.getBounds(),
        zoom: wc.getZoomFactor(),
      };
      // 先确认 JS 线程还活着（最简单的表达式），再量事件循环延迟。
      const alive = await probe(win, "1+1", 3000);
      facts.renderer = { alive: alive === 2 };
      if (alive === 2) facts.renderer = { ...(await probe(win, LAG_SCRIPT, 5000) as object | null) ?? {}, alive: true };
    }

    try {
      // 每个进程的 CPU / 内存：卡死时这里能看出是主进程、渲染进程还是某个子进程在烧 CPU。
      facts.metrics = app.getAppMetrics().map((m) => ({
        pid: m.pid,
        type: m.type,
        name: m.name || "",
        cpu_percent: Math.round(m.cpu.percentCPUUsage * 10) / 10,
        idle_wakeups: m.cpu.idleWakeupsPerSecond,
        memory_mb: Math.round(m.memory.workingSetSize / 1024),
      }));
    } catch {
      facts.metrics = [];
    }

    const file = path.join(opts.logsDir, `diagnostic-${now.toISOString().replace(/[:.]/g, "-")}.json`);
    try {
      fs.mkdirSync(opts.logsDir, { recursive: true });
      fs.writeFileSync(file, JSON.stringify(facts, null, 2), "utf-8");
    } catch (e) {
      return { ok: false, summary: `写诊断文件失败：${(e as Error).message}` };
    }

    const w = facts.window as { crashed?: boolean; focused?: boolean } | null;
    const r = facts.renderer as { alive?: boolean; lag_ms?: number; has_focus?: boolean } | null;
    const parts = [
      !w ? "没有窗口" : w.crashed ? "渲染进程已崩溃" : r?.alive === false ? "渲染进程无响应（JS 线程卡住）" : "渲染进程有响应",
      r?.lag_ms !== undefined ? `事件循环延迟 ${r.lag_ms}ms` : "",
      r?.has_focus !== undefined ? (r.has_focus ? "窗口有焦点" : "窗口无焦点") : "",
    ].filter(Boolean);
    return { ok: true, file, summary: parts.join("；") };
  };

  ipcMain.handle("diag:snapshot", () => collect());
  return { collect };
}
