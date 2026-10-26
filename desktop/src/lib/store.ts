import { create } from "zustand";
import type { ModelInfo, ThreadDetail, ThreadSummary, Workspace } from "../types";
import { applyItemNotification } from "./reduce";
import { RpcClient, type ConnState } from "./rpc";

export type View = "chat" | "settings";
export type Theme = "system" | "dark" | "light";
export type RightTab = "review" | "terminal" | "files";
export type SettingsSection = "general" | "models" | "tools" | "mcp" | "about";

type State = {
  conn: ConnState;
  connError: string | null;
  rpc: RpcClient | null;
  view: View;
  theme: Theme;
  workspaces: Workspace[];
  threads: ThreadSummary[];
  details: Record<string, ThreadDetail>;
  activeThreadId: string | null;
  activeWorkspace: string | null;
  turnStartedAt: Record<string, number>;
  models: ModelInfo[];
  defaultModel: string;
  draftModel: string | null;
  draftMode: string;
  rightOpen: boolean;
  rightTab: RightTab;
  reviewNonce: number;
  filePath: string | null; // 文件标签页当前预览的文件（相对工作区；null = 显示文件树）
  lastFilePath: string | null;
  fileNonce: number;
  search: string;
  settingsSection: SettingsSection | null;
  composerDraft: { text: string; nonce: number } | null; // 让输入框预填（分叉后） // 打开设置页时要跳到的分区（斜杠命令用），读取后清空
  toast: { text: string; kind: "info" | "error" } | null;
};

type Actions = {
  init(rpc: RpcClient): Promise<void>;
  refreshLists(): Promise<void>;
  refreshModels(): Promise<void>;
  openThread(id: string): Promise<void>;
  newThread(cwd?: string | null): void;
  addWorkspace(path: string): Promise<void>;
  removeWorkspace(path: string): Promise<void>;
  send(text: string): Promise<void>;
  interrupt(): Promise<void>;
  compact(): Promise<void>;
  undo(): Promise<void>;
  forkFrom(itemId: string): Promise<void>;
  editResend(itemId: string, text: string): Promise<boolean>;
  openSettings(section?: SettingsSection): void;
  respond(callId: string, choice: "once" | "always" | "deny", reason?: string): Promise<void>;
  openFile(path: string): void;
  set(p: Partial<State>): void;
  showToast(text: string, kind?: "info" | "error"): void;
  setTheme(t: Theme): void;
};

export const useApp = create<State & Actions>((set, get) => ({
  conn: "connecting",
  connError: null,
  rpc: null,
  view: "chat",
  theme: (localStorage.getItem("ee.theme") as Theme) || "system",
  workspaces: [],
  threads: [],
  details: {},
  activeThreadId: null,
  activeWorkspace: localStorage.getItem("ee.workspace"),
  turnStartedAt: {},
  models: [],
  defaultModel: "",
  draftModel: null,
  draftMode: localStorage.getItem("ee.mode") || "default",
  rightOpen: localStorage.getItem("ee.right") !== "0",
  rightTab: "review",
  reviewNonce: 0,
  filePath: null,
  lastFilePath: null,
  fileNonce: 0,
  search: "",
  settingsSection: null,
  composerDraft: null,
  toast: null,

  set: (p) => set(p),

  openFile(path) {
    const st = get();
    const cwd = (st.activeThreadId ? st.details[st.activeThreadId]?.cwd : null) ?? st.activeWorkspace ?? "";
    const rel = toRelative(path, cwd);
    localStorage.setItem("ee.right", "1");
    set((s) => ({ rightOpen: true, rightTab: "files", view: "chat", filePath: rel, lastFilePath: rel, fileNonce: s.fileNonce + 1 }));
  },

  showToast(text, kind = "info") {
    set({ toast: { text, kind } });
    setTimeout(() => {
      if (get().toast?.text === text) set({ toast: null });
    }, 4000);
  },

  setTheme(t) {
    localStorage.setItem("ee.theme", t);
    set({ theme: t });
    applyTheme(t);
  },

  async init(rpc) {
    set({ rpc });
    let wasOpen = false;
    rpc.onState((s) => {
      set({ conn: s });
      if (s === "open") {
        get().refreshLists().catch(() => undefined);
        // 断线重连后：重新拉当前会话，补上断线期间错过的条目
        const id = get().activeThreadId;
        if (wasOpen && id) {
          rpc.call("thread/get", { thread_id: id })
            .then((r) => useApp.setState((st) => ({ details: { ...st.details, [id]: r.thread } })))
            .catch(() => undefined);
        }
        wasOpen = true;
      }
    });
    rpc.onNotification((method, p) => onNotification(method, p));
    rpc.connect();
    await Promise.all([get().refreshLists(), get().refreshModels()]).catch(() => undefined);
    try {
      const s = await rpc.call("settings/get");
      const theme = s.settings.ui.theme as Theme;
      get().setTheme(theme);
      if (!localStorage.getItem("ee.mode")) set({ draftMode: s.settings.permissions.mode });
    } catch {
      /* ignore */
    }
    const { activeWorkspace, workspaces } = get();
    if (!activeWorkspace && workspaces[0]) set({ activeWorkspace: workspaces[0].path });
  },

  async refreshLists() {
    const rpc = get().rpc!;
    const [ws, th] = await Promise.all([rpc.call("workspace/list"), rpc.call("thread/list", { limit: 300 })]);
    set({ workspaces: ws.workspaces, threads: th.threads });
  },

  async refreshModels() {
    const rpc = get().rpc!;
    const r = await rpc.call("models/list");
    set({ models: r.models, defaultModel: r.default });
  },

  async openThread(id) {
    const rpc = get().rpc!;
    set({ activeThreadId: id, view: "chat" });
    try {
      const r = await rpc.call("thread/get", { thread_id: id });
      const t = r.thread as ThreadDetail;
      set((s) => ({ details: { ...s.details, [id]: t }, activeWorkspace: t.cwd }));
      localStorage.setItem("ee.workspace", t.cwd);
    } catch (e) {
      get().showToast((e as Error).message, "error");
    }
  },

  newThread(cwd) {
    const target = cwd ?? get().activeWorkspace;
    if (target) localStorage.setItem("ee.workspace", target);
    set({ activeThreadId: null, view: "chat", activeWorkspace: target ?? null, draftModel: null });
  },

  async addWorkspace(path) {
    const rpc = get().rpc!;
    const r = await rpc.call("workspace/open", { path });
    localStorage.setItem("ee.workspace", path);
    set({ workspaces: r.workspaces, activeWorkspace: r.workspaces[0]?.path ?? path, activeThreadId: null, view: "chat" });
  },

  async removeWorkspace(path) {
    const rpc = get().rpc!;
    const r = await rpc.call("workspace/remove", { path });
    set((s) => ({ workspaces: r.workspaces, activeWorkspace: s.activeWorkspace === path ? null : s.activeWorkspace }));
  },

  async send(text) {
    const s = get();
    const rpc = s.rpc!;
    let id = s.activeThreadId;
    const model = s.draftModel || undefined;
    const mode = s.draftMode;
    try {
      if (!id) {
        if (!s.activeWorkspace) {
          s.showToast("先在左侧选择或添加一个项目文件夹", "error");
          return;
        }
        const r = await rpc.call("thread/start", { cwd: s.activeWorkspace, model, mode });
        const t = r.thread as ThreadDetail;
        id = t.id;
        set((st) => ({
          details: { ...st.details, [t.id]: t },
          activeThreadId: t.id,
          threads: [t, ...st.threads.filter((x) => x.id !== t.id)],
        }));
      }
      set((st) => ({ turnStartedAt: { ...st.turnStartedAt, [id!]: st.turnStartedAt[id!] ?? Date.now() } }));
      setStatus(id, "running");
      setStatusText(id, "正在发送…");
      await rpc.call("turn/start", { thread_id: id, text, model, mode });
    } catch (e) {
      const msg = (e as Error).message;
      get().showToast(msg, "error");
      if (id) {
        // 让对话里也看得到：不再停在“正在工作”
        setStatus(id, "idle");
        setStatusText(id, "");
        useApp.setState((st) => {
          const d = st.details[id!];
          if (!d) return {};
          const err = { id: `local_err_${Date.now()}`, type: "error", message: `发送失败：${msg}`, ts: Date.now() / 1000 } as any;
          const rest = { ...st.turnStartedAt };
          delete rest[id!];
          return { details: { ...st.details, [id!]: { ...d, items: [...d.items, err] } }, turnStartedAt: rest };
        });
      }
    }
  },

  async compact() {
    const s = get();
    const id = s.activeThreadId;
    const d = id ? s.details[id] : null;
    if (!id || !d) {
      s.showToast("还没有对话，不需要压缩");
      return;
    }
    if (d.status !== "idle") {
      s.showToast("正在运行中，等这一回合结束再压缩", "error");
      return;
    }
    setStatus(id, "compacting");
    try {
      const r = await s.rpc!.call("thread/compact", { thread_id: id }, 300000);
      if (r.context_left !== undefined && r.context_left !== null) {
        useApp.setState((st) => (st.details[id] ? { details: { ...st.details, [id]: { ...st.details[id], context_left: r.context_left } } } : {}));
      }
      get().showToast(r.message || (r.ok ? "已压缩上下文" : "没有压缩"), r.ok ? "info" : "error");
    } catch (e) {
      get().showToast((e as Error).message, "error");
    } finally {
      setStatus(id, "idle");
    }
  },

  async undo() {
    const s = get();
    const id = s.activeThreadId;
    const d = id ? s.details[id] : null;
    if (!id || !d) {
      s.showToast("还没有对话，没有可撤销的改动");
      return;
    }
    if (d.status !== "idle") {
      s.showToast("正在运行中，等这一回合结束再撤销", "error");
      return;
    }
    try {
      const r = await s.rpc!.call("thread/undo", { thread_id: id });
      s.showToast(r.restored?.length ? `已恢复 ${r.restored.length} 个文件` : "上一回合没有可撤销的文件改动");
      set((st) => ({ reviewNonce: st.reviewNonce + 1 }));
    } catch (e) {
      s.showToast((e as Error).message, "error");
    }
  },

  async forkFrom(itemId) {
    const s = get();
    const id = s.activeThreadId;
    if (!id) return;
    try {
      const r = await s.rpc!.call("thread/fork", { thread_id: id, item_id: itemId });
      const t = r.thread as ThreadDetail;
      set((st) => ({
        details: { ...st.details, [t.id]: t },
        threads: [t, ...st.threads.filter((x) => x.id !== t.id)],
        activeThreadId: t.id,
        view: "chat",
        composerDraft: { text: r.draft ?? "", nonce: Date.now() },
      }));
      s.showToast("已分叉到新对话，可以修改后发送");
    } catch (e) {
      s.showToast((e as Error).message, "error");
    }
  },

  async editResend(itemId, text) {
    const s = get();
    const id = s.activeThreadId;
    if (!id) return false;
    try {
      set((st) => ({ turnStartedAt: { ...st.turnStartedAt, [id]: Date.now() } }));
      const r = await s.rpc!.call("thread/edit_resend", {
        thread_id: id,
        item_id: itemId,
        text,
        model: s.draftModel || undefined,
        mode: s.draftMode,
      });
      const t = r.thread as ThreadDetail;
      set((st) => ({ details: { ...st.details, [id]: { ...t, status: "running" } }, reviewNonce: st.reviewNonce + 1 }));
      if (r.restored?.length) s.showToast(`已恢复 ${r.restored.length} 个文件并重新发送`);
      return true;
    } catch (e) {
      set((st) => {
        const rest = { ...st.turnStartedAt };
        delete rest[id];
        return { turnStartedAt: rest };
      });
      s.showToast((e as Error).message, "error");
      return false;
    }
  },

  openSettings(section) {
    set({ view: "settings", settingsSection: section ?? null });
  },

  async interrupt() {
    const id = get().activeThreadId;
    if (!id) return;
    await get().rpc!.call("turn/interrupt", { thread_id: id }).catch(() => undefined);
  },

  async respond(callId, choice, reason) {
    const id = get().activeThreadId;
    if (!id) return;
    try {
      await get().rpc!.call("approval/respond", { thread_id: id, call_id: callId, choice, reason: reason || "" });
    } catch (e) {
      get().showToast((e as Error).message, "error");
    }
  },
}));

function setStatusText(id: string, text: string) {
  useApp.setState((s) => (s.details[id] ? { details: { ...s.details, [id]: { ...s.details[id], status_text: text } } } : {}));
}

function setStatus(id: string, status: ThreadSummary["status"]) {
  useApp.setState((s) => ({
    threads: s.threads.map((t) => (t.id === id ? { ...t, status } : t)),
    details: s.details[id] ? { ...s.details, [id]: { ...s.details[id], status } } : s.details,
  }));
}

function onNotification(method: string, p: any) {
  const st = useApp.getState();
  if (method === "thread/updated") {
    const t = p.thread as ThreadSummary;
    useApp.setState((s) => {
      const exists = s.threads.some((x) => x.id === t.id);
      const threads = exists ? s.threads.map((x) => (x.id === t.id ? { ...x, ...t } : x)) : [t, ...s.threads];
      const details = s.details[t.id] ? { ...s.details, [t.id]: { ...s.details[t.id], ...t } } : s.details;
      return { threads, details };
    });
    return;
  }
  if (method === "thread/deleted") {
    useApp.setState((s) => ({
      threads: s.threads.filter((x) => x.id !== p.thread_id),
      activeThreadId: s.activeThreadId === p.thread_id ? null : s.activeThreadId,
    }));
    return;
  }
  if (method === "settings/changed") {
    st.refreshModels().catch(() => undefined);
    return;
  }
  if (method === "turn/started") {
    useApp.setState((s) => ({ turnStartedAt: { ...s.turnStartedAt, [p.thread_id]: s.turnStartedAt[p.thread_id] ?? Date.now() } }));
  }
  if (method === "turn/completed") {
    useApp.setState((s) => {
      const rest = { ...s.turnStartedAt };
      delete rest[p.thread_id];
      return { turnStartedAt: rest, reviewNonce: s.reviewNonce + 1 };
    });
  }
  if (method === "item/updated" && p.item?.type === "tool" && p.item?.meta?.kind === "edit") {
    useApp.setState((s) => ({ reviewNonce: s.reviewNonce + 1 }));
  }
  const tid = p?.thread_id;
  if (!tid) return;
  const d = useApp.getState().details[tid];
  if (!d) return;
  useApp.setState((s) => ({ details: { ...s.details, [tid]: applyItemNotification(d, method, p) } }));
}

/** 工作区内的绝对路径转成相对路径（统一用 /）；不在工作区里的原样返回，交给后端判断。 */
export function toRelative(path: string, cwd: string): string {
  const p = path.replace(/\\/g, "/").replace(/^\.\//, "");
  const c = cwd.replace(/\\/g, "/").replace(/\/+$/, "");
  if (c && p.toLowerCase().startsWith(c.toLowerCase() + "/")) return p.slice(c.length + 1);
  return p;
}

export function applyTheme(t: Theme) {
  const dark = t === "dark" || (t === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  window.ee?.setTheme(t).catch(() => undefined);
}
