// 打包前守门：ee-backend 必须存在，而且不能比 Python 源码旧。
//
// 为什么需要它：`resources/backend/` 里的后端是 PyInstaller 打出来的独立 exe，
// 由 `scripts/build-backend.mjs` 生成，**不会被 vite/tsc 顺手重建**。改完 Python
// 代码直接 `npm run dist:win`，装出来的客户端跑的还是几天前的后端——今天修的
// 东西一条都不在里面，而安装包看起来完全正常。
//
// 2026-11-05 实测就是这种情况：exe 停在 11-01，而 ee/ 下一直在改。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const exeName = process.platform === "win32" ? "ee-backend.exe" : "ee-backend";
const exe = path.join(desktopDir, "resources", "backend", "ee-backend", exeName);
const srcDir = path.join(desktopDir, "..", "ee", "ee");

function newestPy(dir) {
  let newest = 0;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name === "__pycache__") continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) newest = Math.max(newest, newestPy(p));
    else if (e.name.endsWith(".py")) newest = Math.max(newest, fs.statSync(p).mtimeMs);
  }
  return newest;
}

if (!fs.existsSync(exe)) {
  console.error(`[check-backend] 缺后端：${exe}`);
  console.error(`  先跑  node scripts/build-backend.mjs  或  npm run dist:win:full`);
  process.exit(1);
}

const exeTime = fs.statSync(exe).mtimeMs;
const srcTime = newestPy(srcDir);
if (exeTime < srcTime) {
  console.error("[check-backend] 后端比源码旧——这个安装包会带旧后端。");
  console.error(`  后端：${new Date(exeTime).toLocaleString()}`);
  console.error(`  源码：${new Date(srcTime).toLocaleString()}`);
  console.error("  先跑  node scripts/build-backend.mjs  重建后端，或用 npm run dist:win:full");
  process.exit(1);
}

console.log(`[check-backend] ok：${exe}`);
