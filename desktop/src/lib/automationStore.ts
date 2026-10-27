// 自动化（D4-10）的前端状态：自动化列表、运行历史（收件箱）、未读数。独立于主 store，
// 自己订阅后端通知 automation/*；运行结束时弹 Windows 通知（kind=automation，点通知打开那次运行的会话）；
// 打开某个会话时如果它是一次未读的运行，自动标为已读。
import { create } from "zustand";
import type { RpcClient } from "./rpc";
import { automationNotice, type Automation, type AutomationRun } from "./schedule";
import { useLoops } from "./loopStore";
import { useApp } from "./store";

type State = {
  automations: Automation[];
  runs: AutomationRun[];
  unread: number;
  loaded: boolean;
  editing: (Partial<Automation> & { fromLoop?: { id: string; threadId: string } }) | null; // 新建/编辑弹窗：null=关闭；没有 id = 新建；fromLoop：由 /loop 转来，保存后停掉那个循环
  filter: string; // 运行记录只看某个自动化（空 = 全部）
};

type Actions = {
  init(rpc: RpcClient): () => void;
  refresh(): Promise<void>;
  save(a: Partial<Automation> & { fromLoop?: { id: string; threadId: string } }): Promise<boolean>;
  setEnabled(id: string, enabled: boolean): Promise<void>;
  remove(id: string): Promise<void>;
  runNow(id: string): Promise<void>;
  markRead(p: { run_ids?: string[]; thread_id?: string; all?: boolean }): Promise<void>;
  openRun(run: AutomationRun): Promise<void>;
  worktreeAction(runId: string, action: "merge" | "squash" | "discard"): Promise<boolean>;
  set(p: Partial<State>): void;
};

const rpc = () => useApp.getState().rpc!;
const toast = (t: string, k: "info" | "error" = "info") => useApp.getState().showToast(t, k);

function upsert<T extends { id: string }>(list: T[], x: T): T[] {
  return list.some((y) => y.id === x.id) ? list.map((y) => (y.id === x.id ? { ...y, ...x } : y)) : [x, ...list];
}

export const useAutomations = create<State & Actions>((set, get) => ({
  automations: [],
  runs: [],
  unread: 0,
  loaded: false,
  editing: null,
  filter: "",
  set: (p) => set(p),

  init(client) {
    const offN = client.onNotification((method, p: any) => {
      if (method === "automation/updated" && p?.automation) {
        set((s) => ({ automations: upsert(s.automations, p.automation as Automation) }));
      } else if (method === "automation/deleted") {
        set((s) => ({
          automations: s.automations.filter((a) => a.id !== p.automation_id),
          runs: s.runs.filter((r) => r.automation_id !== p.automation_id),
          unread: p.unread ?? s.unread,
        }));
      } else if (method === "automation/run_updated" && p?.run) {
        set((s) => ({ runs: upsert(s.runs, p.run as AutomationRun).sort((a, b) => b.started - a.started) }));
      } else if (method === "automation/run_done" && p?.run) {
        const run = p.run as AutomationRun;
        set((s) => ({ unread: p.unread ?? s.unread, runs: upsert(s.runs, run).sort((a, b) => b.started - a.started) }));
        // 正在看这次运行的会话：直接标已读，不弹通知
        const st = useApp.getState();
        if (run.thread_id && st.activeThreadId === run.thread_id && st.view === "chat" && document.hasFocus()) {
          get().markRead({ run_ids: [run.id] }).catch(() => undefined);
          return;
        }
        const n = automationNotice(run);
        if (n) window.ee?.notify?.(n);
      } else if (method === "automation/read") {
        const ids = new Set<string>(p.run_ids ?? []);
        set((s) => ({ unread: p.unread ?? s.unread, runs: s.runs.map((r) => (ids.has(r.id) ? { ...r, unread: false } : r)) }));
      }
    });
    const offState = client.onState((st) => {
      if (st === "open") get().refresh().catch(() => undefined);
    });
    // 打开的会话是一次未读运行 → 标已读（点收件箱、点通知、点左栏都会走到这里）
    const offActive = useApp.subscribe((s, prev) => {
      if (s.activeThreadId && s.activeThreadId !== prev.activeThreadId) {
        const id = s.activeThreadId;
        if (get().runs.some((r) => r.thread_id === id && r.unread)) get().markRead({ thread_id: id }).catch(() => undefined);
      }
    });
    get().refresh().catch(() => undefined);
    return () => {
      offN();
      offState();
      offActive();
    };
  },

  async refresh() {
    const [l, r] = await Promise.all([rpc().call("automations/list"), rpc().call("automations/runs", { limit: 200 })]);
    set({ automations: l.automations, runs: r.runs, unread: r.unread, loaded: true });
  },

  async save(a) {
    try {
      const { id, fromLoop, ...rest } = a;
      const body = {
        name: rest.name,
        cwd: rest.cwd,
        prompt: rest.prompt,
        model: rest.model ?? "",
        mode: rest.mode,
        approve: rest.approve,
        worktree: !!rest.worktree,
        schedule: rest.schedule,
        enabled: rest.enabled ?? true,
        timeout_minutes: rest.timeout_minutes ?? 60,
      };
      const r = id
        ? await rpc().call("automations/update", { id, patch: body })
        : await rpc().call("automations/create", { automation: body });
      set((s) => ({ automations: upsert(s.automations, r.automation), editing: null }));
      if (fromLoop) {
        // 由会话内 /loop 转来：持久化后停掉原来的循环，免得同一件事跑两份
        await rpc().call("loop/stop", { thread_id: fromLoop.threadId, id: fromLoop.id }).catch(() => undefined);
        useLoops.getState().drop([fromLoop.id]);
        toast("已转为自动化，原来的会话内循环已停止");
      } else toast(id ? "已保存" : "已创建自动化");
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    }
  },

  async setEnabled(id, enabled) {
    try {
      const r = await rpc().call("automations/update", { id, patch: { enabled } });
      set((s) => ({ automations: upsert(s.automations, r.automation) }));
    } catch (e) {
      toast((e as Error).message, "error");
    }
  },

  async remove(id) {
    try {
      await rpc().call("automations/delete", { id });
      set((s) => ({ automations: s.automations.filter((a) => a.id !== id), runs: s.runs.filter((r) => r.automation_id !== id) }));
    } catch (e) {
      toast((e as Error).message, "error");
    }
  },

  async runNow(id) {
    try {
      const r = await rpc().call("automations/run_now", { id });
      set((s) => ({ runs: upsert(s.runs, r.run).sort((a, b) => b.started - a.started) }));
      toast("已开始运行，结果会出现在下面的运行记录里");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  },

  async markRead(p) {
    const r = await rpc().call("automations/mark_read", p);
    const ids = new Set<string>(r.changed ?? []);
    set((s) => ({ unread: r.unread, runs: s.runs.map((x) => (ids.has(x.id) ? { ...x, unread: false } : x)) }));
  },

  async worktreeAction(runId, action) {
    try {
      const r = await rpc().call("automations/worktree", { run_id: runId, action }, 180000);
      set((s) => ({ runs: upsert(s.runs, r.run) }));
      toast(action === "discard" ? "已丢弃这次运行的改动" : action === "squash" ? "改动已放进主目录暂存区，确认后自己提交" : "已合并到主目录");
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    }
  },

  async openRun(run) {
    if (!run.thread_id) {
      if (run.unread) await get().markRead({ run_ids: [run.id] }).catch(() => undefined);
      return;
    }
    await useApp.getState().openThread(run.thread_id);
    if (run.unread) await get().markRead({ run_ids: [run.id] }).catch(() => undefined);
  },
}));
