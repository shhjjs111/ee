import { create } from "zustand";
import type { ModelInfo, ThreadDetail, ThreadSummary, Workspace } from "../types";
import { applyItemNotification } from "./reduce";
import { RpcClient, type ConnState } from "./rpc";

export type View = "chat" | "settings";
export type Theme = "system" | "dark" | "light";
export type RightTab = "review" | "terminal" | "files";

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
  search: string;
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
  respond(callId: string, choice: "once" | "always" | "deny", reason?: string): Promise<void>;
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
  search: "",
  toast: null,

  set: (p) => set(p),

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
    rpc.onState((s) => {
      set({ conn: s });
      if (s === "open") get().refreshLists().catch(() => undefined);
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
      await rpc.call("turn/start", { thread_id: id, text, model, mode });
    } catch (e) {
      get().showToast((e as Error).message, "error");
    }
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

export function applyTheme(t: Theme) {
  const dark = t === "dark" || (t === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  window.ee?.setTheme(t).catch(() => undefined);
}
