import { contextBridge, ipcRenderer, webUtils } from "electron";

contextBridge.exposeInMainWorld("ee", {
  platform: process.platform,
  /** 文件的真实磁盘路径。Electron 32 起 `File.path` 被移除，官方做法就是在 preload 里
   *  用 `webUtils.getPathForFile`（https://www.electronjs.org/docs/latest/api/web-utils）。
   *  JS 里 new 出来的 File 没有磁盘对应，返回空串；不是 File 会抛错，一律兜成空串，
   *  由渲染进程决定提示什么（拖入的压缩包之类不该插进输入框）。 */
  getFilePath: (f: File) => {
    try {
      return webUtils.getPathForFile(f) || "";
    } catch {
      return "";
    }
  },
  getBackend: () => ipcRenderer.invoke("backend:info"),
  restartBackend: () => ipcRenderer.invoke("backend:restart"),
  openFolder: () => ipcRenderer.invoke("dialog:openFolder"),
  openPath: (p: string) => ipcRenderer.invoke("shell:openPath", p),
  showItem: (p: string) => ipcRenderer.invoke("shell:showItem", p),
  openExternal: (url: string) => ipcRenderer.invoke("shell:openExternal", url),
  setTheme: (t: string) => ipcRenderer.invoke("app:setTheme", t),
  appInfo: () => ipcRenderer.invoke("app:info"),
  // 自动更新：拉当前快照 + 收增量 + 手动检查 + 重启安装
  update: {
    state: () => ipcRenderer.invoke("update:state"),
    check: () => ipcRenderer.invoke("update:check"),
    install: () => ipcRenderer.invoke("update:install"),
    openRelease: () => ipcRenderer.invoke("update:open"),
    onState: (cb: (s: unknown) => void) => {
      const h = (_e: unknown, s: unknown) => cb(s);
      ipcRenderer.on("update:state", h);
      return () => ipcRenderer.removeListener("update:state", h);
    },
  },
  captureWindow: () => ipcRenderer.invoke("ui:capture"),
  /** 图片另存为：主进程弹保存对话框并写盘（渲染进程没有文件系统权限）。 */
  saveImage: (name: string, dataUrl: string) => ipcRenderer.invoke("dialog:saveImage", name, dataUrl),
  log: (line: string) => ipcRenderer.send("log:write", line),
  term: {
    available: () => ipcRenderer.invoke("term:available"),
    create: (opts: { cwd?: string; shell?: { kind?: string | null; exe?: string | null }; cols?: number; rows?: number }) =>
      ipcRenderer.invoke("term:create", opts),
    write: (id: number, data: string) => ipcRenderer.send("term:write", id, data),
    resize: (id: number, cols: number, rows: number) => ipcRenderer.send("term:resize", id, cols, rows),
    kill: (id: number) => ipcRenderer.invoke("term:kill", id),
    onData: (cb: (m: { id: number; data: string }) => void) => {
      const h = (_e: unknown, m: { id: number; data: string }) => cb(m);
      ipcRenderer.on("term:data", h);
      return () => ipcRenderer.removeListener("term:data", h);
    },
    onExit: (cb: (m: { id: number; code: number }) => void) => {
      const h = (_e: unknown, m: { id: number; code: number }) => cb(m);
      ipcRenderer.on("term:exit", h);
      return () => ipcRenderer.removeListener("term:exit", h);
    },
  },
  notify: (msg: { kind: string; threadId: string; title: string; body: string }) => ipcRenderer.send("app:notify", msg),
  setPrefs: (p: { notify: string; notifyApproval: boolean; closeToTray: boolean }) => ipcRenderer.invoke("app:setPrefs", p),
  setTrayStatus: (s: { running: number; waiting: number }) => ipcRenderer.send("app:trayStatus", s),
  quitApp: () => ipcRenderer.send("app:quit"),
  onOpenThread: (cb: (threadId: string) => void) => {
    const h = (_e: unknown, id: string) => cb(id);
    ipcRenderer.on("app:openThread", h);
    return () => ipcRenderer.removeListener("app:openThread", h);
  },
  onTrayAction: (cb: (action: string) => void) => {
    const h = (_e: unknown, a: string) => cb(a);
    ipcRenderer.on("app:trayAction", h);
    return () => ipcRenderer.removeListener("app:trayAction", h);
  },
  onBackendExit: (cb: (info: { code: number | null; tail: string }) => void) => {
    const h = (_e: unknown, info: { code: number | null; tail: string }) => cb(info);
    ipcRenderer.on("backend:exit", h);
    return () => ipcRenderer.removeListener("backend:exit", h);
  },
});
