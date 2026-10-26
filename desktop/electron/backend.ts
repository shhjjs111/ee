// 拉起 Python 后端（ee serve），读 stdout 的 READY 行拿端口；token 由这里生成，经环境变量传给后端。
import { ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

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
}

function devPython(): string {
  if (process.env.EE_PYTHON) return process.env.EE_PYTHON;
  return process.platform === "win32" ? "python" : "python3";
}

export function backendCommand(opts: BackendOptions): { cmd: string; args: string[]; cwd: string; env: NodeJS.ProcessEnv } {
  const base = ["serve", "--exit-on-stdin-eof"];
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
  onExit: ((code: number | null, tail: string) => void) | null = null;

  constructor(private opts: BackendOptions) {}

  info(): Promise<BackendInfo> {
    if (!this.ready) this.ready = this.start();
    return this.ready;
  }

  restart(): Promise<BackendInfo> {
    this.stop();
    this.ready = this.start();
    return this.ready;
  }

  lastError(): string {
    return this.stderrTail.join("");
  }

  private start(): Promise<BackendInfo> {
    const token = randomBytes(24).toString("hex");
    const { cmd, args, cwd, env } = backendCommand(this.opts);
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
        env: { ...process.env, ...env, EE_SERVE_TOKEN: token, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
      this.proc = child;
      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          reject(new Error(`后端 20 秒内没有启动。\n${this.lastError()}`));
        }
      }, 20000);
      let buf = "";
      child.stdout?.on("data", (d: Buffer) => {
        const s = d.toString("utf8");
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
        const s = d.toString("utf8");
        log?.write(s);
        this.stderrTail.push(s);
        if (this.stderrTail.length > 40) this.stderrTail.shift();
      });
      child.on("error", (err) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(`无法启动后端（${cmd}）：${err.message}`));
        }
      });
      child.on("exit", (code) => {
        log?.write(`\n==== 后端退出，code=${code}\n`);
        log?.end();
        if (this.proc === child) this.proc = null;
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(`后端启动失败（退出码 ${code}）。\n${this.lastError()}`));
        } else if (this.onExit) {
          this.onExit(code, this.lastError());
        }
      });
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
    setTimeout(() => {
      if (p.exitCode === null) p.kill();
    }, 1500);
  }
}
