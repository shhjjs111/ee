import { create } from "zustand";
import { approvalNotice, turnCompletedNotice } from "./notify";
import type { BgProc, ChildApprovalNote, ModelInfo, ThreadDetail, ThreadSummary, Workspace } from "../types";
import { applyItemNotification } from "./reduce";
import type { QueueKind } from "./queue";
import type { Attachment } from "./attachments";
import { RpcClient, type ConnState } from "./rpc";


/** 快照恢复只完成一部分（有文件被占用）：如实提示已恢复几个、哪些没恢复。 */
export function partialToast(done: number, failed: { path: string; reason?: string }[]): string {
  const names = failed.slice(0, 5).map((f) => f.path).join("、");
  const more = failed.length > 5 ? ` 等 ${failed.length} 个` : "";
  return `只恢复了 ${done} 个文件，${names}${more} 没恢复（可能被其他程序占用），关掉占用的程序后再试一次`;
}

/** 撤销 / 编辑重发前发现用户在回合后手改过要恢复的文件：确认框文字。 */
export function conflictPrompt(conflicts: string[], action: string): string {
  const shown = conflicts.slice(0, 10).join("\n");
  const more = conflicts.length > 10 ? `\n……等 ${conflicts.length} 个文件` : "";
  return (
    `${action}会把下面这些文件恢复到 ee 改动之前，但它们在 ee 改完之后又被改过（可能是你手动改的）：\n\n${shown}${more}\n\n` +
    "继续的话，恢复前会先把当前状态存成检查点，之后可在时间线里恢复回来。继续吗？"
  );
}

export type View = "chat" | "settings" | "automations";
export type Theme = "system" | "dark" | "light";
export type RightTab = "review" | "terminal" | "files";
export type SettingsSection = "general" | "models" | "answer" | "tools" | "mcp" | "hooks" | "about";

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
  bgProcs: Record<string, BgProc[]>; // 每个会话的后台命令（D4-6）
  /** 子会话阻塞在审批/提问上，按父会话 id 记一条（D6-1 U8）。
   *  用户看的是父会话界面；不留这个标记，子任务会静默卡到超时。 */
  pendingChildApprovals: Record<string, ChildApprovalNote>;
  models: ModelInfo[];
  defaultModel: string;
  draftModel: string | null;
  draftMode: string;
  draftEffort: string | null; // D5-9：新对话首条消息前选的推理强度（null = 按模型默认）
  draftStyle: string | null; // D5-9：新对话首条消息前选的回答风格（null = 设置里的默认）
  defaultEffort: string; // 设置里的全局默认推理强度
  defaultStyle: string; // 设置里的默认回答风格
  draftWorktree: boolean; // 新对话在新 worktree 里开始（D5-1）
  worktreeDialog: string | null; // 打开 worktree 管理的项目路径
  rightOpen: boolean;
  rightTab: RightTab;
  reviewNonce: number;
  filePath: string | null; // 文件标签页当前预览的文件（相对工作区；null = 显示文件树）
  lastFilePath: string | null;
  fileNonce: number;
  search: string;
  settingsSection: SettingsSection | null;
  returnedDraft: { threadId: string; texts: string[]; nonce: number } | null; // 中断 / 撤回退回的排队消息，放回输入框
  composerDraft: { text: string; nonce: number } | null; // 让输入框预填（分叉后） // 打开设置页时要跳到的分区（斜杠命令用），读取后清空
  toast: { text: string; kind: "info" | "error" } | null;
  dialog: { kind: "review"; instructions?: string } | { kind: "commit" } | null; // /review、/commit 弹窗
};

export type ReviewParams = { kind: "uncommitted" | "branch" | "commit" | "custom"; ref?: string; instructions?: string };

type Actions = {
  init(rpc: RpcClient): Promise<void>;
  refreshLists(): Promise<void>;
  /** 补拉子会话真实状态（changed_count / terminal_reason），卡片要用。 */
  refreshChildStatus(): Promise<void>;
  /** 拉项目/对话/模型列表，失败时 toast（连接、重连后用）。 */
  refreshListsSafe(): Promise<void>;
  refreshModels(): Promise<void>;
  openThread(id: string): Promise<void>;
  newThread(cwd?: string | null): void;
  /** 失败时提示错误并返回 false（不抛）。 */
  addWorkspace(path: string): Promise<boolean>;
  removeWorkspace(path: string): Promise<boolean>;
  renameThread(id: string, title: string): Promise<boolean>;
  deleteThread(id: string): Promise<boolean>;
  /** queue：运行中发送时排进哪条队列（steer 插话 / follow 完成后再做）；空闲时忽略。 */
  send(text: string, attachments?: Attachment[], queue?: QueueKind): Promise<boolean>;
  /** 没有当前对话时按草稿（项目、模型、模式、worktree）新建一个，返回会话 id；没选项目返回 null。 */
  ensureThread(): Promise<string | null>;
  withdrawQueued(itemId: string): Promise<void>;
  startReview(p: ReviewParams): Promise<boolean>;
  interrupt(): Promise<void>;
  compact(): Promise<void>;
  undo(): Promise<void>;
  /** D5-9：切换推理强度 / 回答风格。有对话时立即生效（推理强度同时按模型记住），没有对话时记为新对话的初始值。 */
  setOptions(o: { effort?: string; style?: string }): Promise<boolean>;
  forkFrom(itemId: string): Promise<void>;
  editResend(itemId: string, text: string): Promise<boolean>;
  openSettings(section?: SettingsSection): void;
  respond(callId: string, choice: "once" | "always" | "forever" | "deny", reason?: string): Promise<void>;
  answer(callId: string, answer: string): Promise<boolean>;
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
  bgProcs: {},
  pendingChildApprovals: {},
  models: [],
  defaultModel: "",
  draftModel: null,
  draftMode: localStorage.getItem("ee.mode") || "default",
  draftEffort: null,
  draftStyle: null,
  defaultEffort: "",
  defaultStyle: "default",
  draftWorktree: false,
  worktreeDialog: null,
  rightOpen: localStorage.getItem("ee.right") !== "0",
  rightTab: "review",
  reviewNonce: 0,
  filePath: null,
  lastFilePath: null,
  fileNonce: 0,
  search: "",
  settingsSection: null,
  composerDraft: null,
  returnedDraft: null,
  toast: null,
  dialog: null,

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
    // 内核重启后会换一个新的 RpcClient：旧连接迟到的状态 / 通知一律丢弃，
    // 否则旧连接重连失败的 "failed" 会把界面标成断线、甚至再触发一次自动重启
    const current = () => get().rpc === rpc;
    rpc.onState((s) => {
      if (!current()) return;
      set({ conn: s });
      if (s === "open") {
        get().refreshListsSafe();
        // 断线重连后：重新拉当前会话，补上断线期间错过的条目
        const id = get().activeThreadId;
        if (wasOpen && id) {
          rpc.call("thread/get", { thread_id: id })
            .then((r) => useApp.setState((st) => ({ details: { ...st.details, [id]: r.thread } })))
            .catch((e: Error) => get().showToast(`重连后刷新当前对话失败：${e.message}`, "error"));
        }
        wasOpen = true;
      }
    });
    rpc.onNotification((method, p) => {
      if (current()) onNotification(method, p);
    });
    rpc.connect();
    await get().refreshListsSafe();
    try {
      const s = await rpc.call("settings/get");
      const theme = s.settings.ui.theme as Theme;
      get().setTheme(theme);
      pushDesktopPrefs(s.settings.ui);
      if (!localStorage.getItem("ee.mode")) set({ draftMode: s.settings.permissions.mode });
      set({ defaultEffort: s.settings.reasoning?.effort ?? "", defaultStyle: s.settings.style?.default ?? "default" });
    } catch (e) {
      get().showToast(`读取设置失败，先用默认设置：${(e as Error).message}`, "error");
    }
    const { activeWorkspace, workspaces } = get();
    if (!activeWorkspace && workspaces[0]) set({ activeWorkspace: workspaces[0].path });
  },

  async refreshListsSafe() {
    try {
      await Promise.all([get().refreshLists(), get().refreshModels()]);
    } catch (e) {
      get().showToast(`加载项目、对话或模型列表失败：${(e as Error).message}`, "error");
    }
  },

  async refreshLists() {
    const rpc = get().rpc!;
    const [ws, th] = await Promise.all([rpc.call("workspace/list"), rpc.call("thread/list", { limit: 300 })]);
    set({ workspaces: ws.workspaces, threads: th.threads });
    void get().refreshChildStatus();
  },

  /** 拉子会话的真实状态（D6-1 U8/U9）。
   *
   *  `thread/updated` 只带 summary() 里有的字段，`changed_count` 要现算（读 worktree 的
   *  git status），拿不到就永远是空的——卡片上的「N 处改动」得靠这一步补。
   *  逐个子会话查：一个坏了不影响其他（thread/status 对坏的 worktree 返回 0）。
   */
  async refreshChildStatus() {
    const rpc = get().rpc;
    if (!rpc) return;
    const kids = get().threads.filter((t) => t.orchestrator_parent_id);
    if (!kids.length) return;
    const results = await Promise.all(kids.map(async (t) => {
      try {
        const r = await rpc.call("thread/status", { parent_id: t.orchestrator_parent_id, child_id: t.id });
        return r?.thread ?? null;
      } catch {
        return null;   // 父会话已删/子会话不在：静默跳过，不能让一次失败拖垮整个列表
      }
    }));
    const byId = new Map(results.filter(Boolean).map((x) => [x.id as string, x]));
    if (!byId.size) return;
    set((s) => ({
      threads: s.threads.map((t) => (byId.has(t.id) ? { ...t, ...byId.get(t.id)! } : t)),
    }));
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
      // 迟到的响应（用户已经切到别的对话）只缓存详情，不改当前项目
      const still = get().activeThreadId === id;
      set((s) => ({ details: { ...s.details, [id]: t }, ...(still ? { activeWorkspace: t.cwd } : {}) }));
      if (still) localStorage.setItem("ee.workspace", t.cwd);
      rpc.call("shell/list", { thread_id: id })
        .then((r) => set((s) => ({ bgProcs: { ...s.bgProcs, [id]: r.procs as BgProc[] } })))
        .catch((e: Error) => get().activeThreadId === id && get().showToast(`读取后台命令失败：${e.message}`, "error"));
    } catch (e) {
      if (get().activeThreadId === id) get().showToast((e as Error).message, "error");
    }
  },

  newThread(cwd) {
    const target = cwd ?? get().activeWorkspace;
    if (target) localStorage.setItem("ee.workspace", target);
    set({ activeThreadId: null, view: "chat", activeWorkspace: target ?? null, draftModel: null });
  },

  async addWorkspace(path) {
    try {
      const r = await get().rpc!.call("workspace/open", { path });
      const p = r.workspaces[0]?.path ?? path; // 后端规范化后的路径（Windows 上盘符 / 分隔符）
      localStorage.setItem("ee.workspace", p);
      set({ workspaces: r.workspaces, activeWorkspace: p, activeThreadId: null, view: "chat" });
      return true;
    } catch (e) {
      get().showToast((e as Error).message, "error");
      return false;
    }
  },

  async removeWorkspace(path) {
    try {
      const r = await get().rpc!.call("workspace/remove", { path });
      set((s) => ({ workspaces: r.workspaces, activeWorkspace: s.activeWorkspace === path ? null : s.activeWorkspace }));
      return true;
    } catch (e) {
      get().showToast((e as Error).message, "error");
      return false;
    }
  },

  async renameThread(id, title) {
    try {
      await get().rpc!.call("thread/rename", { thread_id: id, title });
      return true;
    } catch (e) {
      get().showToast(`重命名失败：${(e as Error).message}`, "error");
      return false;
    }
  },

  async deleteThread(id) {
    try {
      await get().rpc!.call("thread/delete", { thread_id: id });
      forgetThread(id);
      return true;
    } catch (e) {
      get().showToast(`删除失败：${(e as Error).message}`, "error");
      return false;
    }
  },

  async ensureThread() {
    const s = get();
    if (s.activeThreadId) return s.activeThreadId;
    if (!s.activeWorkspace) {
      s.showToast("先在左侧选择或添加一个项目文件夹", "error");
      return null;
    }
    const model = s.draftModel || undefined;
    const r = await s.rpc!.call("thread/start", { cwd: s.activeWorkspace, model, mode: s.draftMode, worktree: s.draftWorktree || undefined }, s.draftWorktree ? 120000 : undefined);
    const t = r.thread as ThreadDetail;
    set((st) => ({
      draftWorktree: false,
      details: { ...st.details, [t.id]: t },
      activeThreadId: t.id,
      threads: [t, ...st.threads.filter((x) => x.id !== t.id)],
    }));
    return t.id;
  },

  async send(text, attachments, queue) {
    const s = get();
    const rpc = s.rpc!;
    let id = s.activeThreadId;
    const model = s.draftModel || undefined;
    const mode = s.draftMode;
    let queuing = false;
    try {
      if (!id) {
        id = await get().ensureThread();
        if (!id) return false;
      }
      queuing = !!queue && (get().details[id]?.status ?? "idle") !== "idle";
      if (!queuing) {
        set((st) => ({ turnStartedAt: { ...st.turnStartedAt, [id!]: st.turnStartedAt[id!] ?? Date.now() } }));
        setStatus(id, "running");
        setStatusText(id, "正在发送…");
      }
      const atts = attachments?.length ? attachments.map((a) => ({ name: a.name, data: a.data, thumb: a.thumb })) : undefined;
      const effort = s.activeThreadId ? undefined : (s.draftEffort ?? undefined);
      const style = s.activeThreadId ? undefined : (s.draftStyle ?? undefined);
      if (!s.activeThreadId) set({ draftEffort: null, draftStyle: null });
      await rpc.call("turn/start", { thread_id: id, text, model, mode, attachments: atts, effort, style, queue }, atts ? 120000 : undefined);
      return true;
    } catch (e) {
      const msg = (e as Error).message;
      get().showToast(msg, "error");
      if (id && queuing) {
        // 排队失败：回合还在跑，不动状态，把文字放回输入框
        set({ returnedDraft: { threadId: id, texts: [text], nonce: Date.now() } });
        return false;
      }
      // 输入框已经清空了：没带图片的消息把文字退回输入框，免得用户重打（带图片的由输入框自己恢复）
      if (!attachments?.length && text.trim()) {
        if (id) set({ returnedDraft: { threadId: id, texts: [text], nonce: Date.now() } });
        else set({ composerDraft: { text, nonce: Date.now() } });
      }
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
      return false;
    }
  },

  async startReview(p) {
    const s = get();
    const rpc = s.rpc!;
    let id = s.activeThreadId;
    const model = s.draftModel || undefined;
    try {
      const cur = id ? s.details[id]?.status : undefined;
      if (cur && cur !== "idle") {
        s.showToast("ee 正在工作，等这一回合结束再开始审查", "error");
        return false;
      }
      if (!id) {
        if (!s.activeWorkspace) {
          s.showToast("先在左侧选择或添加一个项目文件夹", "error");
          return false;
        }
        const r = await rpc.call("thread/start", { cwd: s.activeWorkspace, model, mode: s.draftMode });
        const t = r.thread as ThreadDetail;
        id = t.id;
        set((st) => ({
          details: { ...st.details, [t.id]: t },
          activeThreadId: t.id,
          threads: [t, ...st.threads.filter((x) => x.id !== t.id)],
        }));
      }
      await rpc.call("review/start", { thread_id: id, kind: p.kind, ref: p.ref ?? "", instructions: p.instructions ?? "", model });
      set((st) => ({ turnStartedAt: { ...st.turnStartedAt, [id!]: st.turnStartedAt[id!] ?? Date.now() } }));
      setStatus(id, "running");
      return true;
    } catch (e) {
      get().showToast((e as Error).message, "error");
      return false;
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

  async setOptions(o) {
    const s = get();
    const id = s.activeThreadId;
    if (!id) {
      set({ ...(o.effort !== undefined ? { draftEffort: o.effort } : {}), ...(o.style !== undefined ? { draftStyle: o.style } : {}) });
      return true;
    }
    try {
      const r = await s.rpc!.call("thread/options", { thread_id: id, ...o });
      useApp.setState((st) => {
        const d = st.details[id];
        const patch = { effort: r.effort, style: r.style, effort_support: r.effort_support };
        return {
          details: d ? { ...st.details, [id]: { ...d, ...patch } } : st.details,
          threads: st.threads.map((x) => (x.id === id ? { ...x, ...patch } : x)),
        };
      });
      return true;
    } catch (e) {
      s.showToast((e as Error).message, "error");
      return false;
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
      let r = await s.rpc!.call("thread/undo", { thread_id: id });
      if (r.needs_confirm) {
        if (!confirm(conflictPrompt(r.conflicts ?? [], "撤销上一回合"))) return;
        r = await s.rpc!.call("thread/undo", { thread_id: id, force: true });
      }
      if (r.partial) s.showToast(partialToast(r.restored?.length ?? 0, r.failed ?? []), "error");
      else
        s.showToast(
          r.restored?.length
            ? `已恢复 ${r.restored.length} 个文件${r.restore_point ? "（恢复前的状态已存为检查点，可在时间线里恢复回来）" : ""}`
            : "上一回合没有可撤销的文件改动",
        );
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
      const params = { thread_id: id, item_id: itemId, text, model: s.draftModel || undefined, mode: s.draftMode };
      let r = await s.rpc!.call("thread/edit_resend", params);
      if (r.needs_confirm) {
        if (!confirm(conflictPrompt(r.conflicts ?? [], "编辑重发"))) {
          set((st) => {
            const rest = { ...st.turnStartedAt };
            delete rest[id];
            return { turnStartedAt: rest };
          });
          return false;
        }
        r = await s.rpc!.call("thread/edit_resend", { ...params, force: true });
      }
      const t = r.thread as ThreadDetail;
      set((st) => ({ details: { ...st.details, [id]: { ...t, status: "running" } }, reviewNonce: st.reviewNonce + 1 }));
      if (r.failed?.length) s.showToast(partialToast(r.restored?.length ?? 0, r.failed), "error");
      else if (r.restored?.length) s.showToast(`已恢复 ${r.restored.length} 个文件并重新发送`);
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

  async withdrawQueued(itemId) {
    const id = get().activeThreadId;
    if (!id) return;
    try {
      const r = await get().rpc!.call("turn/withdraw", { thread_id: id, item_id: itemId });
      set({ returnedDraft: { threadId: id, texts: [r.text ?? ""], nonce: Date.now() } });
    } catch (e) {
      get().showToast((e as Error).message, "error");
    }
  },

  async interrupt() {
    const id = get().activeThreadId;
    if (!id) return;
    await get()
      .rpc!.call("turn/interrupt", { thread_id: id })
      .catch((e: Error) => get().showToast(`停止失败：${e.message}`, "error"));
  },

  async answer(callId, answer) {
    const id = get().activeThreadId;
    if (!id) return false;
    try {
      await get().rpc!.call("question/respond", { thread_id: id, call_id: callId, answer });
      return true;
    } catch (e) {
      get().showToast((e as Error).message, "error");
      return false;
    }
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

/** 会话删除后统一清理：列表、详情、后台命令、计时缓存都不留幽灵数据。 */
function forgetThread(id: string) {
  useApp.setState((s) => {
    const details = { ...s.details };
    delete details[id];
    const bgProcs = { ...s.bgProcs };
    delete bgProcs[id];
    const turnStartedAt = { ...s.turnStartedAt };
    delete turnStartedAt[id];
    return {
      details,
      bgProcs,
      turnStartedAt,
      threads: s.threads.filter((x) => x.id !== id),
      activeThreadId: s.activeThreadId === id ? null : s.activeThreadId,
    };
  });
}

function setStatusText(id: string, text: string) {
  useApp.setState((s) => (s.details[id] ? { details: { ...s.details, [id]: { ...s.details[id], status_text: text } } } : {}));
}

function setStatus(id: string, status: ThreadSummary["status"]) {
  useApp.setState((s) => ({
    threads: s.threads.map((t) => (t.id === id ? { ...t, status } : t)),
    details: s.details[id] ? { ...s.details, [id]: { ...s.details[id], status } } : s.details,
  }));
}

export function onNotification(method: string, p: any) {
  const st = useApp.getState();
  if (method === "thread/updated") {
    const t = p.thread as ThreadSummary;
    const prev = st.threads.find((x) => x.id === t.id);
    if (t.status) {
      const n = approvalNotice(t.id, prev?.status, t.status, t.title || prev?.title || "", st.details[t.id]?.items);
      if (n) window.ee?.notify?.(n);
    }
    useApp.setState((s) => {
      const exists = s.threads.some((x) => x.id === t.id);
      const threads = exists ? s.threads.map((x) => (x.id === t.id ? { ...x, ...t } : x)) : [t, ...s.threads];
      const details = s.details[t.id] ? { ...s.details, [t.id]: { ...s.details[t.id], ...t } } : s.details;
      // 子会话不再等审批（已响应/已跑完）：父会话上那条提示要一起撤掉，
      // 否则卡片永远停在「等待批准」。只撤指向这个子会话的，不影响别的子任务。
      let pendingChildApprovals = s.pendingChildApprovals;
      if (t.status !== "waiting_approval") {
        const stale = Object.entries(pendingChildApprovals).find(([, n]) => n.thread_id === t.id);
        if (stale) {
          pendingChildApprovals = { ...pendingChildApprovals };
          delete pendingChildApprovals[stale[0]];
        }
      }
      return { threads, details, pendingChildApprovals };
    });
    return;
  }
  if (method === "thread/child_approval") {
    // 子会话阻塞在审批/提问上（D6-1 U8）：按父会话记一条，并在子会话那一行显示等待态。
    // 用户在看父会话，不把这条带到父界面的话，子任务会静默卡到超时。
    const note = p as ChildApprovalNote;
    useApp.setState((s) => ({
      pendingChildApprovals: { ...s.pendingChildApprovals, [note.parent_id]: note },
      threads: s.threads.map((t) => (t.id === note.thread_id ? { ...t, status: "waiting_approval" as const } : t)),
    }));
    return;
  }
  if (method === "thread/deleted") {
    // 列表从任何地方删掉都要一致：详情/后台命令/计时缓存不能留着（否则会显示已删会话的数据）
    forgetThread(p.thread_id);
    return;
  }
  if (method === "shell/updated") {
    const proc = p.proc as BgProc;
    useApp.setState((s) => {
      const cur = s.bgProcs[p.thread_id] ?? [];
      const next = cur.some((x) => x.id === proc.id) ? cur.map((x) => (x.id === proc.id ? proc : x)) : [...cur, proc];
      return { bgProcs: { ...s.bgProcs, [p.thread_id]: next } };
    });
    return;
  }
  if (method === "git/changed") {
    useApp.setState((s) => ({ reviewNonce: s.reviewNonce + 1 }));
    return;
  }
  if (method === "settings/changed") {
    st.refreshModels().catch((e: Error) => st.showToast(`刷新模型列表失败：${e.message}`, "error"));
    const ss = (p as any)?.settings;
    if (ss) useApp.setState({ defaultEffort: ss.reasoning?.effort ?? "", defaultStyle: ss.style?.default ?? "default" });
    return;
  }
  if (method === "turn/queue_returned") {
    const texts = ((p.items ?? []) as { text: string }[]).map((x) => x.text);
    if (texts.length) {
      const prev = st.returnedDraft && st.returnedDraft.threadId === p.thread_id ? st.returnedDraft.texts : [];
      useApp.setState({ returnedDraft: { threadId: p.thread_id, texts: [...prev, ...texts], nonce: Date.now() } });
    }
  }
  if (method === "turn/started") {
    useApp.setState((s) => ({ turnStartedAt: { ...s.turnStartedAt, [p.thread_id]: s.turnStartedAt[p.thread_id] ?? Date.now() } }));
  }
  if (method === "turn/completed") {
    const title = st.threads.find((x) => x.id === p.thread_id)?.title || st.details[p.thread_id]?.title || "";
    const n = turnCompletedNotice(p, title, st.details[p.thread_id]?.items);
    if (n) window.ee?.notify?.(n);
    useApp.setState((s) => {
      const rest = { ...s.turnStartedAt };
      delete rest[p.thread_id];
      return { turnStartedAt: rest, reviewNonce: s.reviewNonce + 1 };
    });
  }
  if (method === "item/updated" && p.item?.type === "tool" && p.item?.details?.kind === "edit") {
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

/** 把通知 / 托盘设置交给 Electron 主进程（D4-5）。 */
export function pushDesktopPrefs(ui: { notify?: string; notify_approval?: boolean; close_to_tray?: boolean } | undefined) {
  if (!ui) return;
  window.ee?.setPrefs?.({
    notify: (ui.notify as "never" | "unfocused" | "always") ?? "unfocused",
    notifyApproval: ui.notify_approval ?? true,
    closeToTray: ui.close_to_tray ?? true,
  }).catch?.(() => undefined);
}

export function applyTheme(t: Theme) {
  const dark = t === "dark" || (t === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  window.ee?.setTheme(t).catch(() => undefined);
}
