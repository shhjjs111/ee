import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("ee", {
  platform: process.platform,
  getBackend: () => ipcRenderer.invoke("backend:info"),
  restartBackend: () => ipcRenderer.invoke("backend:restart"),
  openFolder: () => ipcRenderer.invoke("dialog:openFolder"),
  openPath: (p: string) => ipcRenderer.invoke("shell:openPath", p),
  showItem: (p: string) => ipcRenderer.invoke("shell:showItem", p),
  openExternal: (url: string) => ipcRenderer.invoke("shell:openExternal", url),
  setTheme: (t: string) => ipcRenderer.invoke("app:setTheme", t),
  appInfo: () => ipcRenderer.invoke("app:info"),
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
