// 自动化（D4-10）的纯函数：调度说明文字、下次运行时间格式化、运行状态文字、完成通知。便于 vitest 测试。
export type Schedule =
  | { kind: "interval"; minutes: number }
  | { kind: "daily"; time: string; weekdays: number[] } // weekdays：0=周日 … 6=周六；空 = 每天
  | { kind: "cron"; expr: string };

export type Automation = {
  id: string;
  name: string;
  cwd: string;
  prompt: string;
  model: string;
  mode: string;
  approve: "deny" | "auto";
  worktree: boolean;
  schedule: Schedule;
  enabled: boolean;
  timeout_minutes: number;
  created?: number;
  updated?: number;
  last_run?: number | null;
  last_status?: string | null;
  next_run?: number | null;
  running?: boolean;
  schedule_text?: string;
};

export type AutomationRun = {
  id: string;
  automation_id: string;
  name: string;
  trigger: "schedule" | "manual" | "catchup";
  started: number;
  finished: number | null;
  status: string; // running | completed | error | max_steps | loop | budget | interrupted | skipped | failed
  thread_id: string | null;
  summary: string;
  unread: boolean;
  worktree?: RunWorktree | null;
  duration_ms?: number;
  usage?: { requests?: number; prompt?: number; completion?: number; hit?: number; cost?: number | null; currency?: string };
  model?: string;
  log?: string; // 完整的结果 / 错误文字（最多 4000 字），失败时也保留
};

/** 一次运行的 worktree（D5-3）：running 运行中 / pending 待处理（合并或丢弃）/ merged / discarded / clean 没改动已清理 / gone 已被别处删除。 */
export type RunWorktree = {
  path: string;
  branch: string;
  name?: string;
  repo?: string;
  state?: "running" | "pending" | "merged" | "discarded" | "clean" | "gone";
  files?: { path: string; additions: number; deletions: number; status?: string }[];
  file_count?: number;
  additions?: number;
  deletions?: number;
  commits?: number;
  merged?: "merge" | "squash";
  error?: string;
};

const WT_STATE: Record<string, string> = {
  running: "运行中",
  pending: "待处理",
  merged: "已合并",
  discarded: "已丢弃",
  clean: "没有改动，已自动清理",
  gone: "worktree 已不存在",
};

export function worktreeStateLabel(w: RunWorktree | null | undefined): string {
  if (!w) return "";
  if (w.state === "merged" && w.merged === "squash") return "已压成一份放进暂存区";
  return WT_STATE[w.state ?? "pending"] ?? w.state ?? "";
}

/** 改动摘要：“3 个文件 +12 −4”；没有改动返回“没有改动”。 */
export function worktreeChangeText(w: RunWorktree | null | undefined): string {
  if (!w) return "";
  const n = w.file_count ?? w.files?.length ?? 0;
  if (!n && !w.commits) return "没有改动";
  return `${n} 个文件 +${w.additions ?? 0} −${w.deletions ?? 0}` + (w.commits ? ` · ${w.commits} 个提交` : "");
}

/** 用时：“45 秒”“3 分 20 秒”“1 小时 5 分”。 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m} 分 ${s % 60} 秒` : `${m} 分`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`;
}

/** 花费：“¥0.0123”；没有单价（null）返回“花费未知”；没有用量返回空。 */
export function formatCost(u: AutomationRun["usage"] | null | undefined): string {
  if (!u) return "";
  if (u.cost == null) return u.prompt || u.completion ? "花费未知" : "";
  const c = Number(u.cost);
  return `${u.currency || "¥"}${c >= 1 ? c.toFixed(2) : c.toFixed(4)}`;
}

/** 运行记录一行的时间信息：“10月27日 09:00 开始 · 用时 3 分 20 秒 · ¥0.0123”。 */
export function runTimeline(r: AutomationRun, now: number = Date.now() / 1000): string {
  const parts = [`${formatNextRun(r.started, now)} 开始`];
  if (r.status === "running") parts.push("进行中");
  else if (r.finished) parts.push(`用时 ${formatDuration(r.duration_ms ?? (r.finished - r.started) * 1000)}`);
  const cost = formatCost(r.usage);
  if (cost) parts.push(cost);
  return parts.join(" · ");
}

export const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** 调度说明，如“每 30 分钟”“工作日 09:00”“每周一、周三 08:30”“cron：0 9 * * 1-5”。与后端 schedule.describe 一致。 */
export function describeSchedule(s: Schedule | null | undefined): string {
  if (!s) return "未设置";
  if (s.kind === "interval") {
    const n = Number(s.minutes) || 0;
    if (n > 0 && n % 1440 === 0) return `每 ${n / 1440} 天`;
    if (n > 0 && n % 60 === 0) return `每 ${n / 60} 小时`;
    return `每 ${n} 分钟`;
  }
  if (s.kind === "daily") {
    const days = [...(s.weekdays ?? [])].sort((a, b) => a - b);
    if (!days.length || days.length === 7) return `每天 ${s.time}`;
    if (days.join(",") === "1,2,3,4,5") return `工作日 ${s.time}`;
    if (days.join(",") === "0,6") return `周末 ${s.time}`;
    return `每${days.map((d) => WEEKDAYS[d] ?? "?").join("、")} ${s.time}`;
  }
  if (s.kind === "cron") return `cron：${s.expr}`;
  return "未知调度";
}

const pad = (n: number) => String(n).padStart(2, "0");

/** 下次运行时间：今天 09:00 / 明天 09:00 / 周三 09:00（一周内）/ 11月2日 09:00 / 2027年1月1日 00:00。 */
export function formatNextRun(ts: number | null | undefined, now: number = Date.now() / 1000): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const n = new Date(now * 1000);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const day0 = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((day0(d) - day0(n)) / 86400000);
  if (diffDays === 0) return `今天 ${hm}`;
  if (diffDays === 1) return `明天 ${hm}`;
  if (diffDays === -1) return `昨天 ${hm}`;
  if (diffDays > 1 && diffDays < 7) return `${WEEKDAYS[d.getDay()]} ${hm}`;
  if (d.getFullYear() === n.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

/** 距离下次运行还有多久：“3 分钟后”“2 小时后”“5 天后”；已过去返回“即将运行”。 */
export function untilText(ts: number | null | undefined, now: number = Date.now() / 1000): string {
  if (!ts) return "";
  const d = ts - now;
  if (d <= 60) return "即将运行";
  if (d < 3600) return `${Math.round(d / 60)} 分钟后`;
  if (d < 86400) return `${Math.round(d / 3600)} 小时后`;
  return `${Math.round(d / 86400)} 天后`;
}

const STATUS: Record<string, { label: string; tone: "ok" | "warn" | "error" | "muted" | "run" }> = {
  running: { label: "运行中", tone: "run" },
  completed: { label: "已完成", tone: "ok" },
  error: { label: "出错", tone: "error" },
  failed: { label: "启动失败", tone: "error" },
  max_steps: { label: "到达步数上限", tone: "warn" },
  loop: { label: "重复操作已停止", tone: "warn" },
  budget: { label: "超出花费上限", tone: "warn" },
  interrupted: { label: "已中断", tone: "warn" },
  skipped: { label: "已跳过", tone: "muted" },
};

export function runStatus(status: string | null | undefined): { label: string; tone: "ok" | "warn" | "error" | "muted" | "run" } {
  return STATUS[status ?? ""] ?? { label: status || "—", tone: "muted" };
}

export const TRIGGER_LABEL: Record<string, string> = { schedule: "按计划", manual: "手动运行", catchup: "错过后补跑" };

/** 运行结束时的桌面通知（kind=automation，点通知打开这次运行的会话）；跳过的不通知。 */
export function automationNotice(run: AutomationRun): { kind: "automation"; threadId: string; title: string; body: string } | null {
  if (!run || run.status === "skipped" || run.status === "running" || !run.thread_id && run.status !== "failed") return null;
  const st = runStatus(run.status);
  return {
    kind: "automation",
    threadId: run.thread_id ?? "",
    title: `自动化 · ${run.name} · ${st.label}`,
    body: (run.summary || "点开查看结果").slice(0, 200),
  };
}

/** 新建自动化时的默认值。 */
export function defaultAutomation(cwd: string): Omit<Automation, "id"> {
  return {
    name: "",
    cwd,
    prompt: "",
    model: "",
    mode: "read-only",
    approve: "deny",
    worktree: false,
    schedule: { kind: "daily", time: "09:00", weekdays: [] },
    enabled: true,
    timeout_minutes: 60,
  };
}

/** 解析“HH:MM”，不合法返回 null。 */
export function parseHHMM(s: string): string | null {
  const m = /^\s*(\d{1,2})[:：](\d{2})\s*$/.exec(s || "");
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  if (h > 23 || mi > 59) return null;
  return `${pad(h)}:${pad(mi)}`;
}
