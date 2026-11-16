// 隔离契约（不需要模型）：临时实例的日志必须写在临时 userData/logs，
// 绝不能写进用户真实的 %APPDATA%\ee\logs。
//
// 背景：`app.getPath("logs")` 默认与 userData 不同源，只 setPath("userData") 时
// backend.log / ee-backend.log 仍然会落到真实日志目录（bot1 审计发现）。
// 修复在 electron/main.ts：EE_TEST_USER_DATA 存在时同时 setPath("logs", userData/logs)。

import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { launchIsolated } from "./launch";

const REAL_LOGS = path.join(process.env.APPDATA || "", "ee", "logs");

/** 只统计文件名与 mtime/大小，不读日志内容。 */
function snapshot(dir: string): { files: number; newest: number; bytes: number } {
  try {
    const names = fs.readdirSync(dir).filter((n) => n.includes(".log"));
    let newest = 0;
    let bytes = 0;
    for (const n of names) {
      const st = fs.statSync(path.join(dir, n));
      newest = Math.max(newest, st.mtimeMs);
      bytes += st.size;
    }
    return { files: names.length, newest, bytes };
  } catch {
    return { files: 0, newest: 0, bytes: 0 };
  }
}

test("临时实例的日志写在临时 userData/logs，真实日志目录不变", async () => {
  const realBefore = snapshot(REAL_LOGS);
  const iso = await launchIsolated();
  try {
    await iso.app.firstWindow();
    // 后端启动时会写 backend.log 头（不必等模型）
    await expect.poll(() => {
      const dir = path.join(iso.userData, "logs");
      return fs.existsSync(dir) && fs.readdirSync(dir).some((n) => n.includes(".log"));
    }, { timeout: 60_000, message: "临时 userData/logs 里应有日志文件" }).toBe(true);

    expect(snapshot(REAL_LOGS), "测试实例不能写用户真实日志目录").toEqual(realBefore);
  } finally {
    await iso.dispose();
  }
});
