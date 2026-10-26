import { app, BrowserWindow, dialog, ipcMain, nativeTheme, shell } from "electron";
import path from "node:path";
import { Backend } from "./backend";
import { killAll as killTerminals, registerTerminalIpc, setPtySearchPaths } from "./terminal";
import { markQuitting, setupTray } from "./tray";

const DEV_URL = process.env.VITE_DEV_SERVER_URL;
let win: BrowserWindow | null = null;
let trayCtl: ReturnType<typeof setupTray> | null = null;

const backend = new Backend({
  isPackaged: app.isPackaged,
  resourcesPath: process.resourcesPath,
  appRoot: path.resolve(__dirname, ".."),
  logFile: path.join(app.getPath("logs"), "backend.log"),
});

setPtySearchPaths(app.isPackaged ? [path.join(process.resourcesPath, "node-pty"), "node-pty"] : ["node-pty"]);
registerTerminalIpc();

backend.onExit = (code, tail) => {
  win?.webContents.send("backend:exit", { code, tail });
};

const OVERLAY = {
  dark: { color: "#00000000", symbolColor: "#c9c9c9", height: 44 },
  light: { color: "#00000000", symbolColor: "#3a3a3a", height: 44 },
};

function createWindow() {
  const dark = nativeTheme.shouldUseDarkColors;
  win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 960,
    minHeight: 600,
    show: false,
    title: "ee",
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
  win.on("closed", () => {
    win = null;
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
ipcMain.handle("shell:openPath", (_e, p: string) => shell.openPath(p));
ipcMain.handle("shell:showItem", (_e, p: string) => shell.showItemInFolder(p));
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
    trayCtl = setupTray({ appRoot: path.resolve(__dirname, ".."), getWin: () => win, createWindow });
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", () => {
  markQuitting();
  trayCtl?.destroy();
  killTerminals();
  backend.stop();
});
