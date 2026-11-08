import { app, BrowserWindow, dialog, ipcMain, nativeTheme, shell } from "electron";
import fs from "node:fs";
import path from "node:path";
import { Backend } from "./backend";
import { allowedRoots, guardedOpenPath, guardedShowItem } from "./openpath";
import { registerDiagIpc } from "./diag";
import { killAll as killTerminals, registerTerminalIpc, setPtySearchPaths } from "./terminal";
import { markQuitting, setupTray } from "./tray";
import { UpdateController, releaseUrl, type UpdateState } from "./updater";

const DEV_URL = process.env.VITE_DEV_SERVER_URL;
let win: BrowserWindow | null = null;
let trayCtl: ReturnType<typeof setupTray> | null = null;

// 测试隔离（必须在 app ready 前调用）：Playwright e2e 起第二个实例时，
// 默认 userData 下的 disk cache 会被正在运行的 ee 锁住（"拒绝访问"），导致
// electron.launch 直接失败。设 EE_TEST_USER_DATA 后实例用独立目录，互不干扰。
if (process.env.EE_TEST_USER_DATA) {
  app.setPath("userData", process.env.EE_TEST_USER_DATA);
}

const backend = new Backend({
  isPackaged: app.isPackaged,
  resourcesPath: process.resourcesPath,
  appRoot: path.resolve(__dirname, ".."),
  logFile: path.join(app.getPath("logs"), "backend.log"),
});

setPtySearchPaths(app.isPackaged ? [path.join(process.resourcesPath, "node-pty"), "node-pty"] : ["node-pty"]);
registerTerminalIpc();
// 卡死诊断（P0-3）：托盘菜单里能一键导出快照，不依赖渲染进程有响应
const diag = registerDiagIpc({ getWin: () => win, logsDir: app.getPath("logs") });

backend.onExit = (code, tail) => {
  win?.webContents.send("backend:exit", { code, tail });
};

const OVERLAY = {
  dark: { color: "#00000000", symbolColor: "#c9c9c9", height: 44 },
  light: { color: "#00000000", symbolColor: "#3a3a3a", height: 44 },
};

/** 窗口/任务栏图标：开发态读 build/，打包后读 resourcesPath（与 tray.ts 一致）。 */
function windowIconPath(): string | undefined {
  const names = process.platform === "win32" ? ["icon.ico", "icon.png"] : ["icon.png"];
  const dirs = app.isPackaged
    ? [process.resourcesPath, path.join(process.resourcesPath, "build")]
    : [path.join(path.resolve(__dirname, ".."), "build")];
  for (const d of dirs) for (const n of names) {
    const p = path.join(d, n);
    if (fs.existsSync(p)) return p;
  }
  return undefined;
}

function createWindow() {
  const dark = nativeTheme.shouldUseDarkColors;
  win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 960,
    minHeight: 600,
    show: false,
    title: "ee",
    icon: windowIconPath(),
    backgroundColor: dark ? "#171717" : "#ffffff",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "hidden",
    titleBarOverlay: process.platform === "darwin" ? undefined : dark ? OVERLAY.dark : OVERLAY.light,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  win.once("ready-to-show", () => win?.show());
  // D4-5：关窗按设置改为隐藏到托盘；窗口回到前台时停止任务栏闪烁
  win.on("close", (e) => {
    if (win && trayCtl?.interceptClose(win)) e.preventDefault();
  });
  win.on("focus", () => win && trayCtl?.stopFlash(win));
  // 卡死自动留证（P0-3）：界面无响应时用户往往连托盘都点不动，靠手点导不出东西。
  // 这两个事件由主进程监听，只要主进程还活着就一定触发，不依赖渲染进程响应。
  let hungAt = 0;
  win.on("unresponsive", () => {
    hungAt = Date.now();
    void diag.collect({ reason: "unresponsive" }).catch(() => undefined);
  });
  // 恢复响应也要记：有这条才知道「卡了多久、自己好了还是没好」，只记开始不够。
  win.on("responsive", () => {
    if (!hungAt) return;
    const secs = Math.round((Date.now() - hungAt) / 1000);
    hungAt = 0;
    void diag.collect({ reason: `responsive-after-${secs}s` }).catch(() => undefined);
  });
  win.on("closed", () => {
    win = null;
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    void diag.collect({ reason: `render-process-gone: ${details.reason}` }).catch(() => undefined);
  });
  // 外部链接用系统浏览器打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (DEV_URL && url.startsWith(DEV_URL)) return;
    e.preventDefault();
    if (/^https?:/.test(url)) shell.openExternal(url);
  });
  if (DEV_URL) {
    win.loadURL(DEV_URL);
  } else {
    win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

ipcMain.handle("backend:info", async () => {
  try {
    return { ok: true, ...(await backend.info()) };
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
});
ipcMain.handle("backend:restart", async () => {
  try {
    return { ok: true, ...(await backend.restart()) };
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
});
ipcMain.handle("dialog:openFolder", async () => {
  const r = await dialog.showOpenDialog(win!, { title: "选择项目文件夹", properties: ["openDirectory", "createDirectory"] });
  return r.canceled ? null : r.filePaths[0] ?? null;
});
ipcMain.handle("shell:openPath", (_e, p: unknown) => guardedOpenPath(p, shell, [...allowedRoots(), app.getPath("logs")])); // 可执行文件只定位不运行；限工作区/ee 目录/日志目录
ipcMain.handle("shell:showItem", (_e, p: unknown) => guardedShowItem(p, shell, [...allowedRoots(), app.getPath("logs")])); // 同一守门：UNC 路径不能让资源管理器去连对方服务器
ipcMain.handle("shell:openExternal", (_e, url: string) => (/^https?:/.test(url) ? shell.openExternal(url) : undefined));
ipcMain.handle("app:setTheme", (_e, theme: "system" | "dark" | "light") => {
  nativeTheme.themeSource = theme;
  const dark = nativeTheme.shouldUseDarkColors;
  if (win && process.platform !== "darwin") {
    try {
      win.setTitleBarOverlay(dark ? OVERLAY.dark : OVERLAY.light);
    } catch {
      /* 老版本不支持 */
    }
  }
  win?.setBackgroundColor(dark ? "#171717" : "#ffffff");
  return dark;
});
ipcMain.on("log:write", (_e, line: string) => {
  if (typeof line === "string") backend.appendLog(line.slice(0, 4000));
});
ipcMain.handle("app:info", () => ({ version: app.getVersion(), logs: app.getPath("logs"), packaged: app.isPackaged }));

// ---- 自动更新（electron-updater，源是 GitHub Releases）
// `electron-updater` 必须延迟 require：它的 ElectronAppAdapter 在构造时就去读
// `resources/app-update.yml`，开发态没有这个文件，顶层加载会在建窗口之前就抛。
// `./updater` 本身只 `import type`，可以正常顶层静态导入。
let updaterCtl: UpdateController | null = null;

/** 推状态给界面。窗口可能已经关到托盘/正在退出，销毁的 webContents 上 send 会抛。 */
function sendUpdate(s: UpdateState) {
  if (!win || win.isDestroyed() || win.webContents.isDestroyed()) return;
  win.webContents.send("update:state", s);
}

function updater(): UpdateController | null {
  if (updaterCtl) return updaterCtl;
  if (!app.isPackaged) return null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { autoUpdater } = require("electron-updater") as typeof import("electron-updater");
    updaterCtl = new UpdateController({
      currentVersion: app.getVersion(),
      packaged: app.isPackaged,
      updater: autoUpdater,
      emit: sendUpdate,
      log: (line) => backend.appendLog(line),
    });
  } catch (e) {
    backend.appendLog(`[更新] 初始化失败：${(e as Error).message}`);
  }
  return updaterCtl;
}

ipcMain.handle("update:state", () => updater()?.getState() ?? { phase: "idle", current: app.getVersion(), message: "开发版不支持自动更新" });
ipcMain.handle("update:check", async () => (await updater()?.check()) ?? { phase: "error", current: app.getVersion(), message: "开发版不支持自动更新" });
ipcMain.handle("update:install", () => updater()?.quitAndInstall() ?? false);
ipcMain.handle("update:open", () => shell.openExternal(releaseUrl()));
// 截图给模型看（后端 screenshot 工具 → notify ui/capture → 这里）：只截 ee 自己的窗口，
// 截不到别的程序；返回 PNG data URL，由渲染进程回送 RPC ui/capture_result。
ipcMain.handle("ui:capture", async () => {
  try {
    if (!win || win.isDestroyed()) return { error: "窗口不存在（可能已关到托盘）" };
    if (win.webContents.isLoading()) return { error: "窗口还在加载" };
    const img = await win.webContents.capturePage();
    if (img.isEmpty()) return { error: "截到的是空图（窗口可能被最小化）" };
    return { data_url: img.toDataURL() };
  } catch (e) {
    return { error: (e as Error).message };
  }
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    } else {
      createWindow();
    }
  });
  app.whenReady().then(() => {
    backend.info().catch(() => undefined); // 尽早启动，窗口加载时并行
    createWindow();
    trayCtl = setupTray({ appRoot: path.resolve(__dirname, ".."), getWin: () => win, createWindow,
                          diag: () => diag.collect() });
    // 开机静默查一次：失败不弹窗，只在日志留一行（见 updater.ts 文件头）
    setTimeout(() => updater()?.start(), 8000);
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
// 退出：先让后端自己清理（结束后台命令、MCP 服务器），最多等 6 秒，超时就结束整棵进程树再真正退出
let backendStopped = false;
app.on("before-quit", (e) => {
  markQuitting();
  trayCtl?.destroy();
  killTerminals();
  if (backendStopped) return;
  e.preventDefault();
  backendStopped = true;
  backend
    .stopAndWait(6000)
    .catch(() => undefined)
    .finally(() => app.quit());
});
