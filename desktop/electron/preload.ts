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
  onBackendExit: (cb: (info: { code: number | null; tail: string }) => void) => {
    const h = (_e: unknown, info: { code: number | null; tail: string }) => cb(info);
    ipcRenderer.on("backend:exit", h);
    return () => ipcRenderer.removeListener("backend:exit", h);
  },
});
