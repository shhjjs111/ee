import { test, expect } from "@playwright/test";
import { launchIsolated } from "./launch";

// 卡死诊断（P0-3）：界面卡住时唯一的现场证据。
// 守两件事：手动导出能写出文件；渲染进程无响应时主进程自己会留证（卡死到连托盘都点不动的情况）。

function diagFiles(app: any): Promise<string[]> {
  return app.evaluate(({ app: a }) => {
    const fs = process.mainModule!.require("fs") as typeof import("fs");
    const logs = a.getPath("logs");
    if (!fs.existsSync(logs)) return [];
    return fs.readdirSync(logs).filter((f) => f.startsWith("diagnostic-"));
  });
}

test("手动导出：写出带内容的诊断文件", async () => {
  const { app, dispose } = await launchIsolated();
  try {
    const r = await app.evaluate(async ({ ipcMain }) => {
      const map = (ipcMain as any)._invokeHandlers as Map<string, Function>;
      const handler = map.get("diag:snapshot");
      if (!handler) return null;
      return await handler({}, undefined);
    });
    expect(r, "diag:snapshot 必须已注册").not.toBeNull();
    expect(r.ok).toBe(true);
    expect(r.file).toContain("diagnostic-");
    expect(r.summary).toContain("渲染进程");

    const files = await diagFiles(app);
    expect(files.length).toBe(1);
    expect(files[0]).toContain("manual");
  } finally {
    await dispose();
  }
});

test("无响应时自动留证，恢复后也记一笔（含卡了多久）", async () => {
  const { app, dispose } = await launchIsolated();
  try {
    await app.evaluate(async ({ BrowserWindow }) => {
      for (let i = 0; i < 50 && BrowserWindow.getAllWindows().length === 0; i++) {
        await new Promise((r) => setTimeout(r, 100));
      }
      const w = BrowserWindow.getAllWindows()[0];
      if (!w) throw new Error("没有窗口");
      w.emit("unresponsive");
      await new Promise((r) => setTimeout(r, 1100));
      w.emit("responsive");
      await new Promise((r) => setTimeout(r, 1500));
    });
    const files = await diagFiles(app);
    expect(files.some((f) => f.includes("unresponsive")), `应有 unresponsive 快照：${files}`).toBe(true);
    // 只记开始不够：要知道是自己好了还是没好，以及卡了多久
    expect(files.some((f) => /responsive-after-\d+s/.test(f)), `应有恢复快照：${files}`).toBe(true);
  } finally {
    await dispose();
  }
});
