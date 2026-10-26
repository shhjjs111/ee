// 内置终端的会话管理：xterm 实例放在 React 之外（切换右侧标签页 / 关掉面板时不销毁），
// 每个会话一个独立的 holder div，挂到当前可见的容器上即可复用。
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { create } from "zustand";
import { useApp } from "./store";

export type TermTab = {
  key: number;
  title: string;
  cwd: string;
  shell: string;
  status: "starting" | "running" | "exited" | "error";
  error?: string;
  code?: number;
};

type Live = { term: Terminal; fit: FitAddon; holder: HTMLDivElement; ptyId: number | null; opened: boolean };

type TermState = {
  tabs: TermTab[];
  active: number | null;
  available: { ok: boolean; error?: string } | null;
};

export const useTerms = create<TermState>(() => ({ tabs: [], active: null, available: null }));

const live = new Map<number, Live>();
const byPty = new Map<number, number>(); // ptyId -> key
let seq = 0;
let wired = false;

const LIGHT = {
  black: "#1d1d1b", red: "#c4262e", green: "#1a7f37", yellow: "#9a6700", blue: "#2f6fed", magenta: "#8250df",
  cyan: "#1b7c83", white: "#6e7781", brightBlack: "#6b6b66", brightRed: "#d1242f", brightGreen: "#2da44e",
  brightYellow: "#bf8700", brightBlue: "#218bff", brightMagenta: "#a475f9", brightCyan: "#3192aa", brightWhite: "#8c959f",
};
const DARK = {
  black: "#484f58", red: "#ff7b72", green: "#56d364", yellow: "#e3b341", blue: "#79a6ff", magenta: "#d2a8ff",
  cyan: "#56d4dd", white: "#d6d6d1", brightBlack: "#6f6f6a", brightRed: "#ffa198", brightGreen: "#7ee2a0",
  brightYellow: "#f2cc60", brightBlue: "#a5c3ff", brightMagenta: "#e2c5ff", brightCyan: "#8ae6ee", brightWhite: "#ffffff",
};

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function termTheme() {
  const dark = document.documentElement.classList.contains("dark");
  const bg = cssVar("--bg") || (dark ? "#181818" : "#ffffff");
  const fg = cssVar("--fg") || (dark ? "#ececea" : "#1d1d1b");
  return {
    background: bg,
    foreground: fg,
    cursor: fg,
    cursorAccent: bg,
    selectionBackground: dark ? "rgba(91,140,255,0.35)" : "rgba(47,111,237,0.22)",
    scrollbarSliderBackground: dark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.12)",
    scrollbarSliderHoverBackground: dark ? "rgba(255,255,255,0.2)" : "rgba(0,0,0,0.2)",
    ...(dark ? DARK : LIGHT),
  };
}

function wire() {
  if (wired || !window.ee?.term) return;
  wired = true;
  const t = window.ee.term;
  t.onData(({ id, data }) => {
    const key = byPty.get(id);
    if (key !== undefined) live.get(key)?.term.write(data);
  });
  t.onExit(({ id, code }) => {
    const key = byPty.get(id);
    byPty.delete(id);
    if (key === undefined) return;
    const l = live.get(key);
    if (l) {
      l.ptyId = null;
      l.term.write(`\r\n\x1b[2m[进程已退出，代码 ${code}。按回车重新启动]\x1b[0m\r\n`);
    }
    patch(key, { status: "exited", code });
  });
  // 跟随亮/暗主题
  new MutationObserver(() => {
    const th = termTheme();
    for (const l of live.values()) l.term.options.theme = th;
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
}

function patch(key: number, p: Partial<TermTab>) {
  useTerms.setState((s) => ({ tabs: s.tabs.map((t) => (t.key === key ? { ...t, ...p } : t)) }));
}

export async function checkAvailable() {
  if (useTerms.getState().available) return useTerms.getState().available!;
  const r = window.ee?.term ? await window.ee.term.available() : { ok: false, error: "需要在 ee 桌面应用里使用（浏览器调试模式没有终端）。" };
  useTerms.setState({ available: r });
  return r;
}

function shellName(file: string, kind: string): string {
  if (kind === "pwsh") return "pwsh";
  if (kind === "powershell") return "powershell";
  if (kind === "git-bash") return "bash";
  if (kind === "cmd") return "cmd";
  return (file.split(/[\\/]/).pop() || kind || "shell").replace(/\.exe$/i, "");
}

/** 新建一个终端标签（还没挂到 DOM；由 attach() 打开并启动进程）。 */
export function newTab(cwd: string): number {
  wire();
  const key = ++seq;
  const term = new Terminal({
    fontFamily: cssVar("--font-mono") || "Cascadia Code, Consolas, monospace",
    fontSize: 12.5,
    lineHeight: 1.25,
    cursorBlink: true,
    cursorStyle: "bar",
    scrollback: 5000,
    allowProposedApi: false,
    theme: termTheme(),
    windowsPty: window.ee?.platform === "win32" ? { backend: "conpty" } : undefined,
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  const holder = document.createElement("div");
  holder.className = "ee-term-holder";
  holder.dataset.key = String(key);
  live.set(key, { term, fit, holder, ptyId: null, opened: false });
  const name = cwd.split(/[\\/]/).filter(Boolean).pop() || cwd;
  useTerms.setState((s) => ({ tabs: [...s.tabs, { key, title: name, cwd, shell: "", status: "starting" }], active: key }));

  term.onData((d) => {
    const l = live.get(key);
    if (!l) return;
    if (l.ptyId !== null) window.ee?.term?.write(l.ptyId, d);
    else if (d === "\r") {
      const tab = useTerms.getState().tabs.find((t) => t.key === key);
      if (tab && tab.status === "exited") {
        term.reset();
        spawn(key).catch(() => undefined);
      }
    }
  });
  term.onResize(({ cols, rows }) => {
    const l = live.get(key);
    if (l?.ptyId != null) window.ee?.term?.resize(l.ptyId, cols, rows);
  });
  // Ctrl+C 有选区时复制、Ctrl+V 粘贴（Windows 终端的习惯）
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && !e.altKey && e.key.toLowerCase() === "c" && term.hasSelection()) {
      navigator.clipboard?.writeText(term.getSelection()).catch(() => undefined);
      term.clearSelection();
      return false;
    }
    if (mod && !e.altKey && e.key.toLowerCase() === "v") {
      navigator.clipboard?.readText().then((t) => t && term.paste(t)).catch(() => undefined);
      return false;
    }
    return true;
  });
  return key;
}

async function spawn(key: number) {
  const l = live.get(key);
  const tab = useTerms.getState().tabs.find((t) => t.key === key);
  if (!l || !tab || !window.ee?.term) return;
  patch(key, { status: "starting", error: undefined });
  let shell: { kind?: string | null; exe?: string | null } | undefined;
  try {
    const rpc = useApp.getState().rpc;
    if (rpc) shell = await rpc.call("terminal/shell", { cwd: tab.cwd }, 8000);
  } catch {
    shell = undefined; // 主进程会按平台兜底
  }
  const r = await window.ee.term.create({ cwd: tab.cwd, shell, cols: l.term.cols, rows: l.term.rows });
  if (!r.ok) {
    patch(key, { status: "error", error: r.error });
    l.term.write(`\x1b[31m${r.error}\x1b[0m\r\n`);
    return;
  }
  l.ptyId = r.id;
  byPty.set(r.id, key);
  patch(key, { status: "running", shell: shellName(r.shell, r.kind), cwd: r.cwd });
}

/** 把会话挂到容器上；第一次挂载时打开 xterm 并启动 shell。 */
export function attach(key: number, container: HTMLElement) {
  const l = live.get(key);
  if (!l) return;
  if (l.holder.parentElement !== container) container.appendChild(l.holder);
  if (!l.opened) {
    l.opened = true;
    l.term.open(l.holder);
    try {
      l.fit.fit();
    } catch {
      /* 尺寸为 0 时 */
    }
    spawn(key).catch((e) => patch(key, { status: "error", error: String(e) }));
  } else {
    fit(key);
  }
  l.term.focus();
}

export function fit(key: number) {
  const l = live.get(key);
  if (!l || !l.opened || !l.holder.isConnected || l.holder.clientWidth === 0) return;
  try {
    l.fit.fit();
  } catch {
    /* ignore */
  }
}

export function focus(key: number) {
  live.get(key)?.term.focus();
}

export function clear(key: number) {
  live.get(key)?.term.clear();
}

export async function closeTab(key: number) {
  const l = live.get(key);
  if (l) {
    if (l.ptyId !== null) {
      byPty.delete(l.ptyId);
      await window.ee?.term?.kill(l.ptyId).catch(() => undefined);
    }
    l.term.dispose();
    l.holder.remove();
    live.delete(key);
  }
  useTerms.setState((s) => {
    const idx = s.tabs.findIndex((t) => t.key === key);
    const tabs = s.tabs.filter((t) => t.key !== key);
    const active = s.active === key ? (tabs[Math.min(idx, tabs.length - 1)]?.key ?? null) : s.active;
    return { tabs, active };
  });
}
