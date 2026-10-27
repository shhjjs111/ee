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

/** 允许打开的根目录：ee 目录 + desktop.json 里登记的工作区。 */
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
  if (!path.isAbsolute(p)) return { action: "deny", reason: "只能打开绝对路径" };
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
