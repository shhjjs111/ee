// “用默认程序打开”的守门：可执行/脚本类文件不直接运行，改为在资源管理器里定位；只允许工作区和 ee 目录里的路径。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Windows 上双击即运行（或交给脚本宿主执行）的扩展名
export const EXECUTABLE_EXTS = new Set([
  ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".ps1xml", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
  ".msc", ".msi", ".msp", ".scr", ".pif", ".lnk", ".url", ".hta", ".cpl", ".jar", ".reg", ".inf", ".scf", ".application",
  ".gadget", ".appref-ms", ".appx", ".msix", ".appxbundle", ".msixbundle", ".sh", ".py", ".pyw", ".pl", ".rb", ".chm",
  ".ws", ".settingcontent-ms", ".library-ms", ".search-ms", ".diagcab", ".xbap", ".dll", ".sys", ".drv", ".ocx",
]);

export type OpenDecision =
  | { action: "open"; path: string }
  | { action: "reveal"; path: string; reason: string }
  | { action: "deny"; reason: string };

/** ee 的用户目录（与内核 ee_home() 一致：EE_HOME 或 ~/.ee）。 */
export function eeHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.EE_HOME ? path.resolve(env.EE_HOME) : path.join(os.homedir(), ".ee");
}

/**
 * 允许打开的根目录：ee 目录 + desktop.json 登记的工作区 + 历史会话里出现过的目录。
 *
 * 为什么要把会话 cwd 也算进来：侧栏显示的项目是从 thread 的 cwd 推导的，
 * 不一定都登记在 desktop.json 里。只读 desktop.json 时，界面上看得见的项目
 * 点「在资源管理器中打开」会被直接拒绝，用户看到的就是「点了没反应」。
 */
export function allowedRoots(home: string = eeHome()): string[] {
  const roots = [home];
  try {
    const d = JSON.parse(fs.readFileSync(path.join(home, "desktop.json"), "utf-8"));
    for (const w of Array.isArray(d?.workspaces) ? d.workspaces : []) {
      if (w && typeof w.path === "string" && w.path) roots.push(w.path);
    }
  } catch {
    /* 没有 desktop.json：只有 ee 目录 */
  }

  // 历史会话目录：~/.ee/sessions/*.jsonl，每行一个事件，首行含 cwd
  try {
    const dir = path.join(home, "sessions");
    const files = fs.readdirSync(dir).filter((f) => f.endsWith(".jsonl")).slice(-200);
    const seen = new Set<string>();
    for (const f of files) {
      try {
        // 只读开头一段：会话首行就带 cwd，没必要整文件读
        const fd = fs.openSync(path.join(dir, f), "r");
        const buf = Buffer.alloc(4096);
        const n = fs.readSync(fd, buf, 0, buf.length, 0);
        fs.closeSync(fd);
        const head = buf.subarray(0, n).toString("utf-8");
        const m = /"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(head);
        if (m) {
          const cwd = JSON.parse(`"${m[1]}"`);
          if (cwd && !seen.has(cwd)) {
            seen.add(cwd);
            roots.push(cwd);
          }
        }
      } catch {
        /* 单个会话读不出来不影响其他 */
      }
    }
  } catch {
    /* 没有 sessions 目录：跳过 */
  }

  return roots;
}

function norm(p: string, win: boolean): string {
  let r = path.resolve(p);
  try {
    r = fs.realpathSync.native(r); // 防符号链接/联接点跳出工作区
  } catch {
    /* 不存在的路径按字面判断 */
  }
  return win ? r.toLowerCase() : r;
}

export function isInside(child: string, root: string, win = process.platform === "win32"): boolean {
  const c = norm(child, win);
  const r = norm(root, win);
  const rel = path.relative(r, c);
  return rel === "" || (!!rel && !rel.startsWith("..") && !path.isAbsolute(rel));
}

export function isExecutablePath(p: string): boolean {
  // 去掉 Windows 文件名末尾会被忽略的点和空格（"a.bat." / "a.bat " 依然会被当 .bat 运行）
  const base = path.basename(p).replace(/[. ]+$/, "");
  return EXECUTABLE_EXTS.has(path.extname(base).toLowerCase());
}

export function decideOpen(p: unknown, roots: string[]): OpenDecision {
  if (typeof p !== "string" || !p.trim()) return { action: "deny", reason: "路径为空" };
  if (/^\\\\|^\/\//.test(p)) return { action: "deny", reason: "不支持打开网络路径" };
  if (!isAbsolutePath(p)) return { action: "deny", reason: "只能打开绝对路径" };
  const abs = path.resolve(p);
  if (!roots.some((r) => r && isInside(abs, r))) return { action: "deny", reason: "只能打开当前工作区或 ee 目录里的文件" };
  let isDir = false;
  try {
    isDir = fs.statSync(abs).isDirectory();
  } catch {
    return { action: "deny", reason: "文件不存在" };
  }
  if (!isDir && isExecutablePath(abs)) {
    return { action: "reveal", path: abs, reason: `为安全起见不直接运行 ${path.extname(abs) || "可执行"} 文件，已在资源管理器中定位` };
  }
  return { action: "open", path: abs };
}

/**
 * Windows 上「绝对路径」的判断不能用 path.isAbsolute。
 *
 * 实测（node 在 win32 上）：
 *   path.isAbsolute("C:\\Users")   === false   ← 盘符根下的一级目录
 *   path.isAbsolute("C:\\Users\\a") === true
 *   path.isAbsolute("d:/ee")       === true
 * 于是 D:\ee日常 这种「盘符根目录」会被误判成相对路径，直接 deny ——
 * 表现就是「点『在资源管理器中打开』没反应」。
 *
 * 这里显式处理盘符形态：X:\... 和 X:/... 都算绝对路径。
 */
export function isAbsolutePath(p: string): boolean {
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;   // C:\... / D:/...
  if (/^[A-Za-z]:$/.test(p)) return false;      // 光秃秃的 "C:" 是盘符当前目录，不是绝对路径
  return path.isAbsolute(p);                    // 其余交给 node（含 UNC、POSIX）
}

/** 主进程 shell:openPath 处理：返回空串表示成功，否则是给界面看的提示（与 shell.openPath 的约定一致）。 */
export async function guardedOpenPath(
  p: unknown,
  sh: { openPath(p: string): Promise<string>; showItemInFolder(p: string): void },
  roots: string[] = allowedRoots(),
): Promise<string> {
  const d = decideOpen(p, roots);
  if (d.action === "deny") return d.reason;
  if (d.action === "reveal") {
    sh.showItemInFolder(d.path);
    return d.reason;
  }
  return sh.openPath(d.path);
}
