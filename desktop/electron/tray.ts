// 桌面通知 + 托盘常驻（D4-5）。
// - 通知：渲染进程把“回合完成 / 出错 / 需要审批”交过来，这里按设置（从不 / 窗口不在前台 / 总是）决定弹不弹；
//   点通知把窗口拉到前台并打开对应会话。需要审批且窗口不在前台时闪烁任务栏图标。
// - 托盘：常驻图标，菜单「打开 ee / 新对话 / 设置 / 退出」，单击显示窗口；提示文字显示进行中的会话数。
// - 关窗：close_to_tray 开着时点 × 只隐藏窗口（任务和后端继续跑），第一次隐藏时提示一次；托盘「退出」才真正退出。
import { app, BrowserWindow, Menu, Notification, Tray, ipcMain, nativeImage } from "electron";
import fs from "node:fs";
import path from "node:path";

export type NotifyMode = "never" | "unfocused" | "always";
export type DesktopPrefs = { notify: NotifyMode; notifyApproval: boolean; closeToTray: boolean };
type NotifyMsg = { kind: string; threadId: string; title: string; body: string };

const prefs: DesktopPrefs = { notify: "unfocused", notifyApproval: true, closeToTray: true };
let tray: Tray | null = null;
let quitting = false;
let hintShown = false;
let status = { running: 0, waiting: 0 };
const live = new Set<Notification>(); // 持有引用，防止被 GC 后点击回调失效

export function isQuitting() {
  return quitting;
}
export function markQuitting() {
  quitting = true;
}

/** 纯函数：该不该弹通知（便于以后单测；主进程逻辑就这一条判断）。 */
export function shouldNotify(p: DesktopPrefs, kind: string, focused: boolean): boolean {
  if (kind === "approval" && !p.notifyApproval) return false;
  if (p.notify === "never") return false;
  if (p.notify === "always") return true;
  return !focused;
}

function iconPath(appRoot: string): string | null {
  const names = process.platform === "win32" ? ["icon.ico", "icon.png"] : ["icon.png"];
  const dirs = app.isPackaged ? [process.resourcesPath, path.join(process.resourcesPath, "build")] : [path.join(appRoot, "build")];
  for (const d of dirs) for (const n of names) {
    const p = path.join(d, n);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function showWindow(getWin: () => BrowserWindow | null, create: () => void) {
  let w = getWin();
  if (!w || w.isDestroyed()) {
    create();
    w = getWin();
  }
  if (!w) return null;
  if (w.isMinimized()) w.restore();
  w.show();
  w.focus();
  return w;
}

function tooltip() {
  const parts: string[] = [];
  if (status.running) parts.push(`${status.running} 个对话在工作`);
  if (status.waiting) parts.push(`${status.waiting} 个等你审批`);
  return parts.length ? `ee — ${parts.join("，")}` : "ee";
}

function buildMenu(getWin: () => BrowserWindow | null, create: () => void) {
  const send = (action: string) => {
    const w = showWindow(getWin, create);
    w?.webContents.send("app:trayAction", action);
  };
  return Menu.buildFromTemplate([
    { label: "打开 ee", click: () => showWindow(getWin, create) },
    { label: "新对话", click: () => send("new") },
    { label: "设置", click: () => send("settings") },
    { type: "separator" },
    { label: tooltip().replace(/^ee — /, "") || "空闲", enabled: false },
    { type: "separator" },
    {
      label: "退出",
      click: () => {
        quitting = true;
        app.quit();
      },
    },
  ]);
}

export function setupTray(opts: { appRoot: string; getWin: () => BrowserWindow | null; createWindow: () => void }) {
  const { appRoot, getWin, createWindow } = opts;
  if (process.platform === "win32") app.setAppUserModelId("com.ee.desktop"); // Windows 通知需要，与 electron-builder appId 一致

  const refresh = () => {
    if (!tray) return;
    tray.setToolTip(tooltip());
    tray.setContextMenu(buildMenu(getWin, createWindow));
  };

  const ensureTray = () => {
    if (tray) return;
    const ip = iconPath(appRoot);
    try {
      const img = ip ? nativeImage.createFromPath(ip) : nativeImage.createEmpty();
      tray = new Tray(img.isEmpty() ? img : img.resize({ width: 16, height: 16 }));
      tray.on("click", () => showWindow(getWin, createWindow));
      tray.on("double-click", () => showWindow(getWin, createWindow));
      refresh();
    } catch {
      tray = null; // 某些 Linux 桌面没有托盘区：忽略，关窗时就照常退出
    }
  };
  ensureTray();

  ipcMain.handle("app:setPrefs", (_e, p: Partial<DesktopPrefs>) => {
    if (p && (p.notify === "never" || p.notify === "unfocused" || p.notify === "always")) prefs.notify = p.notify;
    if (typeof p?.notifyApproval === "boolean") prefs.notifyApproval = p.notifyApproval;
    if (typeof p?.closeToTray === "boolean") prefs.closeToTray = p.closeToTray;
    return { ...prefs };
  });

  ipcMain.on("app:trayStatus", (_e, s: { running: number; waiting: number }) => {
    if (!s) return;
    status = { running: Math.max(0, s.running | 0), waiting: Math.max(0, s.waiting | 0) };
    refresh();
  });

  ipcMain.on("app:quit", () => {
    quitting = true;
    app.quit();
  });

  ipcMain.on("app:notify", (_e, m: NotifyMsg) => {
    if (!m || typeof m.title !== "string") return;
    const w = getWin();
    const focused = !!w && !w.isDestroyed() && w.isVisible() && w.isFocused();
    if (m.kind === "approval" && prefs.notifyApproval && w && !focused) {
      try {
        w.flashFrame(true);
      } catch {
        /* ignore */
      }
    }
    if (!shouldNotify(prefs, m.kind, focused) || !Notification.isSupported()) return;
    const ip = iconPath(appRoot);
    const n = new Notification({
      title: m.title.slice(0, 120),
      body: String(m.body || "").slice(0, 240),
      silent: false,
      ...(ip ? { icon: ip } : {}),
    });
    live.add(n);
    const drop = () => live.delete(n);
    n.on("click", () => {
      drop();
      const win = showWindow(getWin, createWindow);
      if (win && m.threadId) win.webContents.send("app:openThread", m.threadId);
    });
    n.on("close", drop);
    n.on("failed", drop);
    n.show();
    setTimeout(drop, 10 * 60 * 1000);
  });

  return {
    /** 窗口 close 事件里调用：返回 true 表示已改为隐藏到托盘。 */
    interceptClose(win: BrowserWindow): boolean {
      if (quitting || !prefs.closeToTray || !tray) return false;
      win.hide();
      if (!hintShown && Notification.isSupported()) {
        hintShown = true;
        const ip = iconPath(appRoot);
        new Notification({
          title: "ee 仍在后台运行",
          body: "正在跑的任务不会中断。点托盘图标打开，从托盘菜单「退出」彻底关闭（可在设置 → 常规里关掉）。",
          ...(ip ? { icon: ip } : {}),
        }).show();
      }
      return true;
    },
    stopFlash(win: BrowserWindow) {
      try {
        win.flashFrame(false);
      } catch {
        /* ignore */
      }
    },
    destroy() {
      tray?.destroy();
      tray = null;
    },
  };
}
