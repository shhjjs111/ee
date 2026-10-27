// 内置终端：主进程用 node-pty 起真正的伪终端（Windows 上是 ConPTY），通过 IPC 和渲染进程的 xterm.js 对接。
// node-pty 是可选依赖：装不上 / 加载失败时 available() 返回原因，界面降级成提示，不影响其它功能。
import { ipcMain, type WebContents } from "electron";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

type IPty = {
  pid: number;
  onData(cb: (d: string) => void): { dispose(): void };
  onExit(cb: (e: { exitCode: number; signal?: number }) => void): { dispose(): void };
  write(d: string): void;
  resize(cols: number, rows: number): void;
  kill(signal?: string): void;
};
type PtyModule = { spawn(file: string, args: string[], opts: Record<string, unknown>): IPty };

let ptyMod: PtyModule | null = null;
let loadError: string | null = null;

let candidates: string[] = ["node-pty"];

/** 打包后 node-pty 作为 extraResources 放在 resources/node-pty（asar 外，原生模块和 conpty 辅助程序都能直接加载）。 */
export function setPtySearchPaths(paths: string[]) {
  candidates = paths;
}

function loadPty(): PtyModule | null {
  if (ptyMod || loadError) return ptyMod;
  const errs: string[] = [];
  for (const c of candidates) {
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      ptyMod = require(c) as PtyModule;
      return ptyMod;
    } catch (e) {
      errs.push(`${c}: ${(e as Error).message.split("\n")[0]}`);
    }
  }
  loadError = errs.join("；");
  return null;
}

export type ShellSpec = { kind?: string | null; exe?: string | null };

function exists(p: string | undefined | null): p is string {
  if (!p) return false;
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

function which(name: string): string | null {
  const exts = process.platform === "win32" ? (process.env.PATHEXT || ".EXE;.CMD;.BAT").split(";") : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    for (const ext of exts) {
      const p = path.join(dir, name + (name.toLowerCase().endsWith(ext.toLowerCase()) ? "" : ext));
      if (exists(p)) return p;
    }
  }
  return null;
}

/** 后端解析好的 shell（和 agent 的 shell 设置一致）优先；拿不到时按平台兜底。 */
export function resolveShell(spec?: ShellSpec): { file: string; args: string[]; kind: string; env: Record<string, string> } {
  let kind = spec?.kind || "";
  let file = spec?.exe && (exists(spec.exe) || which(spec.exe)) ? (exists(spec.exe) ? spec.exe : which(spec.exe)!) : "";
  if (!file) {
    if (process.platform === "win32") {
      const pwsh = which("pwsh");
      const ps = which("powershell");
      if (pwsh) [file, kind] = [pwsh, "pwsh"];
      else if (ps) [file, kind] = [ps, "powershell"];
      else [file, kind] = [process.env.ComSpec || "cmd.exe", "cmd"];
    } else {
      const sh = process.env.SHELL && exists(process.env.SHELL) ? process.env.SHELL : which("bash") || "/bin/sh";
      [file, kind] = [sh, path.basename(sh)];
    }
  }
  const env: Record<string, string> = {};
  let args: string[] = [];
  if (kind === "pwsh" || kind === "powershell") args = ["-NoLogo"];
  else if (kind === "git-bash") {
    args = ["--login", "-i"];
    env.CHERE_INVOKING = "1"; // 让 --login 保持在当前目录
    env.MSYS = "enable_pcon";
  } else if (kind === "cmd") args = [];
  return { file, args, kind, env };
}

type Session = { pty: IPty; owner: WebContents; buf: string; timer: NodeJS.Timeout | null };
const sessions = new Map<number, Session>();
let seq = 0;

function flush(id: number) {
  const s = sessions.get(id);
  if (!s) return;
  s.timer = null;
  if (!s.buf) return;
  const data = s.buf;
  s.buf = "";
  if (!s.owner.isDestroyed()) s.owner.send("term:data", { id, data });
}

function killSession(id: number) {
  const s = sessions.get(id);
  if (!s) return;
  sessions.delete(id);
  if (s.timer) clearTimeout(s.timer);
  try {
    s.pty.kill();
  } catch {
    /* ignore */
  }
}

/** 每个窗口只挂一次：窗口销毁或页面重新加载（旧页面的终端 id 已经没人认了）时结束它的所有终端。 */
const watchedOwners = new WeakSet<WebContents>();
function watchOwner(owner: WebContents) {
  if (watchedOwners.has(owner)) return;
  watchedOwners.add(owner);
  const killOwned = () => {
    for (const [id, s] of sessions) if (s.owner === owner) killSession(id);
  };
  owner.once("destroyed", killOwned);
  owner.on("did-start-navigation", (details: any, _url?: unknown, isInPlace?: boolean, isMainFrame?: boolean) => {
    const main = details?.isMainFrame ?? isMainFrame;
    const same = details?.isSameDocument ?? isInPlace;
    if (main && !same) killOwned();
  });
}

export function killAll() {
  for (const [id, s] of sessions) {
    try {
      s.pty.kill();
    } catch {
      /* ignore */
    }
    sessions.delete(id);
  }
}

export function registerTerminalIpc() {
  ipcMain.handle("term:available", () => {
    const m = loadPty();
    return m ? { ok: true } : { ok: false, error: loadError || "node-pty 不可用" };
  });

  ipcMain.handle(
    "term:create",
    (e, opts: { cwd?: string; shell?: ShellSpec; cols?: number; rows?: number }) => {
      const m = loadPty();
      if (!m) return { ok: false, error: loadError || "node-pty 不可用" };
      let cwd = opts?.cwd || os.homedir();
      try {
        if (!fs.statSync(cwd).isDirectory()) cwd = os.homedir();
      } catch {
        cwd = os.homedir();
      }
      const sh = resolveShell(opts?.shell);
      const env: Record<string, string> = {};
      for (const [k, v] of Object.entries(process.env)) if (typeof v === "string") env[k] = v;
      // 不把 Electron / 后端口令之类的变量泄进用户终端
      for (const k of Object.keys(env)) if (k.startsWith("ELECTRON_") || k === "EE_SERVE_TOKEN") delete env[k];
      Object.assign(env, sh.env, { TERM: "xterm-256color", COLORTERM: "truecolor", TERM_PROGRAM: "ee" });
      let p: IPty;
      try {
        p = m.spawn(sh.file, sh.args, {
          name: "xterm-256color",
          cols: Math.max(2, Math.floor(opts?.cols || 80)),
          rows: Math.max(1, Math.floor(opts?.rows || 24)),
          cwd,
          env,
          useConpty: true,
        });
      } catch (err) {
        return { ok: false, error: `无法启动 ${sh.file}：${(err as Error).message}` };
      }
      const id = ++seq;
      const s: Session = { pty: p, owner: e.sender, buf: "", timer: null };
      sessions.set(id, s);
      p.onData((d) => {
        s.buf += d;
        if (s.buf.length > 64 * 1024) flush(id);
        else if (!s.timer) s.timer = setTimeout(() => flush(id), 8);
      });
      p.onExit(({ exitCode }) => {
        flush(id);
        sessions.delete(id);
        if (!s.owner.isDestroyed()) s.owner.send("term:exit", { id, code: exitCode });
      });
      watchOwner(e.sender);
      return { ok: true, id, pid: p.pid, shell: sh.file, kind: sh.kind, cwd };
    },
  );

  ipcMain.on("term:write", (_e, id: number, data: string) => {
    sessions.get(id)?.pty.write(data);
  });
  ipcMain.on("term:resize", (_e, id: number, cols: number, rows: number) => {
    const s = sessions.get(id);
    if (!s || !(cols > 0) || !(rows > 0)) return;
    try {
      s.pty.resize(Math.floor(cols), Math.floor(rows));
    } catch {
      /* 进程刚退出时 resize 会抛 */
    }
  });
  ipcMain.handle("term:kill", (_e, id: number) => {
    if (!sessions.has(id)) return false;
    killSession(id);
    return true;
  });
}
