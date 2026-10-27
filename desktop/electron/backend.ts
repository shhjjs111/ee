// 拉起 Python 后端（ee serve），读 stdout 的 READY 行拿端口；token 由这里生成，经环境变量传给后端。
import { ChildProcess, spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { OutputDecoder } from "./textdecode";

export interface BackendInfo {
  port: number;
  token: string;
  pid: number;
}

export interface BackendOptions {
  isPackaged: boolean;
  resourcesPath: string;
  appRoot: string; // desktop/ 目录（开发模式）
  logFile: string;
  /** 测试用：替换拉起命令。 */
  command?: () => { cmd: string; args: string[]; cwd: string; env: NodeJS.ProcessEnv };
  startTimeoutMs?: number; // 默认 20 秒
  stopGraceMs?: number; // stdin 关闭后等多久还没退出就强制结束整棵进程树，默认 5 秒
}

/** 结束一个进程及其子孙进程（Windows 用 taskkill /T，后端的后台命令、MCP 服务器都在这棵树里）。 */
export function killProcessTree(pid: number, sync = false): void {
  if (!pid) return;
  if (process.platform === "win32") {
    try {
      const args = ["/PID", String(pid), "/T", "/F"];
      if (sync) spawnSync("taskkill", args, { stdio: "ignore", windowsHide: true, timeout: 5000 });
      else spawn("taskkill", args, { stdio: "ignore", windowsHide: true }).on("error", () => undefined);
    } catch {
      /* ignore */
    }
    return;
  }
  try {
    process.kill(pid, "SIGKILL");
  } catch {
    /* 已经退出 */
  }
}

function devPython(): string {
  if (process.env.EE_PYTHON) return process.env.EE_PYTHON;
  return process.platform === "win32" ? "python" : "python3";
}

export function backendCommand(opts: BackendOptions): { cmd: string; args: string[]; cwd: string; env: NodeJS.ProcessEnv } {
  // --parent-pid：桌面端被强杀（任务管理器结束进程）时，后端自己发现并退出，同时结束它的子进程
  const base = ["serve", "--exit-on-stdin-eof", "--parent-pid", String(process.pid)];
  if (opts.isPackaged || process.env.EE_BACKEND_EXE) {
    const exeName = process.platform === "win32" ? "ee-backend.exe" : "ee-backend";
    const exe = process.env.EE_BACKEND_EXE || path.join(opts.resourcesPath, "backend", "ee-backend", exeName);
    return { cmd: exe, args: base, cwd: path.dirname(exe), env: {} };
  }
  // 开发：python -m ee serve，PYTHONPATH 指向仓库里的 ee/ 包（即使没 pip install -e 也能跑）
  const eeRoot = path.resolve(opts.appRoot, "..", "ee");
  const sep = process.platform === "win32" ? ";" : ":";
  const pp = [eeRoot, process.env.PYTHONPATH].filter(Boolean).join(sep);
  return { cmd: devPython(), args: ["-m", "ee", ...base], cwd: eeRoot, env: { PYTHONPATH: pp } };
}

export class Backend {
  private proc: ChildProcess | null = null;
  private ready: Promise<BackendInfo> | null = null;
  private stderrTail: string[] = [];
  /** 最近一次拉起的进程 pid（调试 / 测试用）。 */
  lastPid = 0;
  onExit: ((code: number | null, tail: string) => void) | null = null;

  constructor(private opts: BackendOptions) {}

  info(): Promise<BackendInfo> {
    if (!this.ready) this.ready = this.track(this.start());
    return this.ready;
  }

  restart(): Promise<BackendInfo> {
    this.stop();
    this.ready = this.track(this.start());
    return this.ready;
  }

  /** 启动失败不缓存：下次 info() 重新拉起，而不是永远返回同一个失败。 */
  private track(p: Promise<BackendInfo>): Promise<BackendInfo> {
    p.catch(() => {
      if (this.ready === p) this.ready = null;
    });
    return p;
  }

  /** 追加一行到 backend.log（界面侧的 RPC 失败、重连、自动重启等）。 */
  appendLog(line: string): void {
    try {
      fs.mkdirSync(path.dirname(this.opts.logFile), { recursive: true });
      fs.appendFile(this.opts.logFile, `${new Date().toISOString()} [ui] ${line.replace(/\s+$/, "")}\n`, () => undefined);
    } catch {
      /* ignore */
    }
  }

  lastError(): string {
    return this.stderrTail.join("");
  }

  private start(): Promise<BackendInfo> {
    const token = randomBytes(24).toString("hex");
    const { cmd, args, cwd, env } = this.opts.command ? this.opts.command() : backendCommand(this.opts);
    this.stderrTail = [];
    let log: fs.WriteStream | null = null;
    try {
      fs.mkdirSync(path.dirname(this.opts.logFile), { recursive: true });
      log = fs.createWriteStream(this.opts.logFile, { flags: "a" });
      log.write(`\n==== ${new Date().toISOString()} 启动后端：${cmd} ${args.join(" ")}\n`);
    } catch {
      log = null;
    }
    return new Promise<BackendInfo>((resolve, reject) => {
      let settled = false;
      const child = spawn(cmd, args, {
        cwd,
        // EE_LOG_DIR：后端自己的 ee-backend.log 写到同一个 logs 目录（Windows：%APPDATA%\ee\logs）
        env: { ...process.env, ...env, EE_SERVE_TOKEN: token, EE_LOG_DIR: path.dirname(this.opts.logFile), PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" },
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
      this.proc = child;
      this.lastPid = child.pid ?? 0;
      const startMs = this.opts.startTimeoutMs ?? 20000;
      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          // 没启动起来的进程也要结束，否则它可能稍后才起来、一直留在后台
          if (this.proc === child) this.proc = null;
          killProcessTree(child.pid ?? 0);
          reject(new Error(`后端 ${Math.round(startMs / 1000)} 秒内没有启动。\n${this.lastError()}`));
        }
      }, startMs);
      let buf = "";
      const outDec = new OutputDecoder();
      const errDec = new OutputDecoder();
      child.stdout?.on("data", (d: Buffer) => {
        const s = outDec.write(d);
        log?.write(s);
        if (settled) return;
        buf += s;
        const m = buf.match(/EE_SERVE_READY (\{.*\})/);
        if (m) {
          settled = true;
          clearTimeout(timer);
          const j = JSON.parse(m[1]);
          resolve({ port: j.port, token, pid: child.pid ?? 0 });
        }
      });
      child.stderr?.on("data", (d: Buffer) => {
        const s = errDec.write(d);
        log?.write(s);
        this.stderrTail.push(s);
        if (this.stderrTail.length > 40) this.stderrTail.shift();
      });
      child.stdin?.on("error", () => undefined); // 后端先退出时写 stdin 会 EPIPE，别让主进程崩
      child.on("error", (err) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(`无法启动后端（${cmd}）：${err.message}`));
        }
      });
      child.on("exit", (code) => {
        const rest = errDec.end();
        if (rest) this.stderrTail.push(rest);
        log?.write(`${rest}\n==== 后端退出，code=${code}\n`);
        log?.end();
        const current = this.proc === child; // stop()/restart() 结束的旧进程不算“意外退出”
        if (current) this.proc = null;
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(`后端启动失败（退出码 ${code}）。\n${this.lastError()}`));
        } else if (current && this.onExit) {
          this.onExit(code, this.lastError());
        }
      });
    });
  }

  /** 退出应用时用：关 stdin 让后端自己清理，最多等 ms；超时就同步结束整棵进程树。 */
  stopAndWait(ms = 6000): Promise<void> {
    const p = this.proc;
    if (!p || p.exitCode !== null || p.signalCode !== null) {
      this.stop();
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(t);
        resolve();
      };
      const t = setTimeout(() => {
        p.off("exit", done);
        killProcessTree(p.pid ?? 0, true);
        resolve();
      }, ms);
      p.once("exit", done);
      this.stop();
    });
  }

  stop(): void {
    const p = this.proc;
    this.proc = null;
    if (!p) return;
    try {
      p.stdin?.end(); // --exit-on-stdin-eof：后端自己退出
    } catch {
      /* ignore */
    }
    const t = setTimeout(() => {
      // 还没退：强制结束整棵进程树（只 kill 后端本身会留下它起的后台命令 / MCP 服务器）
      if (p.exitCode === null && p.signalCode === null) killProcessTree(p.pid ?? 0);
    }, this.opts.stopGraceMs ?? 5000);
    t.unref?.();
  }
}
