// 自动化（D4-10）：自动化列表（启用开关、立即运行、编辑、删除）+ 运行记录收件箱（未读加粗，点开打开会话并标已读）
// + 新建/编辑弹窗（项目、提示词、调度三选一、模型、权限模式、自动审批、worktree）。
import * as Dialog from "@radix-ui/react-dialog";
import clsx from "clsx";
import { Clock, FileText, GitBranch, GitMerge, History, Layers, Pencil, Play, Plus, Trash2, X } from "lucide-react";
import React, { useEffect, useState } from "react";
import { useAutomations } from "../lib/automationStore";
import {
  TRIGGER_LABEL,
  WEEKDAYS,
  defaultAutomation,
  describeSchedule,
  formatNextRun,
  parseHHMM,
  runStatus,
  runTimeline,
  untilText,
  worktreeChangeText,
  worktreeStateLabel,
  type Automation,
  type AutomationRun,
  type Schedule,
} from "../lib/schedule";
import { useApp } from "../lib/store";
import { Button, Field, Input, Select, Spinner, Toggle, Tip } from "./ui";

const MODES = [
  { value: "read-only", label: "只读（推荐）", hint: "只能读文件和运行只读命令" },
  { value: "default", label: "默认", hint: "需要审批的操作按下面的自动审批策略处理" },
  { value: "auto", label: "全自动", hint: "工作区内随意改；危险命令仍按策略处理" },
  { value: "plan", label: "计划", hint: "只读，只出方案" },
];
const TONE: Record<string, string> = { ok: "text-ok", warn: "text-warn", error: "text-danger", muted: "text-faint", run: "text-accent" };
const base = (p: string) => p.replace(/\\/g, "/").split("/").filter(Boolean).pop() || p;

export function AutomationsPage() {
  const a = useAutomations();
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 30000);
    return () => clearInterval(t);
  }, []);
  const ws = useApp((s) => s.activeWorkspace);
  const runs = a.filter ? a.runs.filter((r) => r.automation_id === a.filter) : a.runs;
  const workspaces = useApp((s) => s.workspaces);

  return (
    <div className="relative flex min-w-0 flex-1 flex-col bg-bg">
      <div className="drag h-11 shrink-0" style={{ marginRight: "var(--overlay-w)" }} />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[860px] px-8 pb-28 pt-2">
          <div className="mb-1 flex items-center gap-3">
            <h2 className="flex-1 text-[20px] font-semibold tracking-tight text-fg">自动化</h2>
            <Button variant="primary" onClick={() => a.set({ editing: defaultAutomation(ws ?? workspaces[0]?.path ?? "") })}>
              <Plus size={14} /> 新建自动化
            </Button>
          </div>
          <p className="mb-5 text-[12.5px] leading-relaxed text-muted">
            按计划在项目里无人值守地跑提示词，每次运行新建一个对话，结果进下面的运行记录并弹通知。关窗后 ee 在托盘继续按计划运行；从托盘彻底退出则暂停，下次启动时错过的会补跑一次。
          </p>

          <section className="mb-6 rounded-2xl border border-line bg-elev">
            {!a.loaded && <div className="flex items-center gap-2 px-5 py-6 text-[13px] text-muted"><Spinner /> 正在加载…</div>}
            {a.loaded && a.automations.length === 0 && (
              <div className="px-5 py-8 text-center text-[12.5px] text-faint">还没有自动化。例如：每个工作日 09:00 检查依赖更新、每晚总结当天的提交。</div>
            )}
            {a.automations.map((x) => (
              <div key={x.id} className="flex items-center gap-3 border-b border-line px-5 py-3 last:border-b-0">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-[13.5px] text-fg">
                    <span className="truncate font-medium">{x.name}</span>
                    {x.running && <Spinner size={12} className="text-accent" />}
                  </div>
                  <div className="mt-0.5 truncate text-[12px] text-faint" title={x.cwd}>
                    {base(x.cwd)} · {describeSchedule(x.schedule)} · {MODES.find((m) => m.value === x.mode)?.label.replace("（推荐）", "") ?? x.mode}
                    {x.worktree ? " · 新 worktree" : ""}
                  </div>
                  <div className="mt-0.5 text-[12px] text-muted">
                    {x.enabled ? `下次：${formatNextRun(x.next_run, now)}（${untilText(x.next_run, now)}）` : "已停用"}
                    {x.last_status && x.last_status !== "running" && <span className={clsx("ml-2", TONE[runStatus(x.last_status).tone])}>上次{runStatus(x.last_status).label}</span>}
                  </div>
                </div>
                <Tip label={x.enabled ? "停用" : "启用"}>
                  <span><Toggle on={x.enabled} onChange={(v) => a.setEnabled(x.id, v)} /></span>
                </Tip>
                <Tip label="只看它的运行历史"><Button size="icon" onClick={() => a.set({ filter: a.filter === x.id ? "" : x.id })}><History size={14} /></Button></Tip>
                <Tip label="立即运行"><Button size="icon" disabled={x.running} onClick={() => a.runNow(x.id)}><Play size={14} /></Button></Tip>
                <Tip label="编辑"><Button size="icon" onClick={() => a.set({ editing: { ...x } })}><Pencil size={14} /></Button></Tip>
                <Tip label="删除">
                  <Button size="icon" variant="danger" onClick={() => confirm(`删除自动化“${x.name}”及其运行记录？（已产生的对话保留）`) && a.remove(x.id)}>
                    <Trash2 size={14} />
                  </Button>
                </Tip>
              </div>
            ))}
          </section>

          <div className="mb-2 flex items-center gap-2">
            <h3 className="flex-1 text-[14px] font-semibold text-fg">运行记录{a.unread ? `（${a.unread} 条未读）` : ""}</h3>
            <Select
              side="bottom"
              className="h-7 border border-line"
              value={a.filter}
              onChange={(v) => a.set({ filter: v })}
              options={[{ value: "", label: "全部自动化" }, ...a.automations.map((x) => ({ value: x.id, label: x.name }))]}
            />
            {a.unread > 0 && <Button size="sm" onClick={() => a.markRead({ all: true })}>全部标为已读</Button>}
          </div>
          <section className="rounded-2xl border border-line bg-elev">
            {runs.length === 0 && <div className="px-5 py-6 text-center text-[12.5px] text-faint">还没有运行记录</div>}
            {runs.map((r) => <RunRow key={r.id} r={r} now={now} />)}
          </section>
        </div>
      </div>
      <EditDialog />
    </div>
  );
}

/** 一条运行记录：状态、触发方式、开始时间 · 用时 · 花费、摘要；worktree 运行显示改动摘要和合并/丢弃；可展开完整日志。 */
export function RunRow({ r, now }: { r: AutomationRun; now: number }) {
  const openRun = useAutomations((s) => s.openRun);
  const act = useAutomations((s) => s.worktreeAction);
  const [showLog, setShowLog] = useState(false);
  const [busy, setBusy] = useState("");
  const st = runStatus(r.status);
  const wt = r.worktree;
  const pending = !!wt && r.status !== "running" && (wt.state ?? "pending") === "pending";
  const go = async (action: "merge" | "squash" | "discard") => {
    if (action === "discard" && !confirm(`丢弃这次运行在 ${wt?.branch} 上的全部改动？（worktree 目录和分支都会删除，找不回来）`)) return;
    setBusy(action);
    await act(r.id, action);
    setBusy("");
  };
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <div
      onClick={() => openRun(r)}
      data-testid="run-row"
      className={clsx("flex cursor-default gap-3 border-b border-line px-5 py-2.5 last:border-b-0 hover:bg-hover", r.thread_id ? "" : "opacity-80")}
    >
      <span className={clsx("mt-1.5 h-2 w-2 shrink-0 rounded-full", r.unread ? "bg-accent" : "bg-transparent")} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-[13px]">
          <span className={clsx("truncate", r.unread ? "font-semibold text-fg" : "text-muted")}>{r.name}</span>
          <span className={clsx("shrink-0 text-[12px]", TONE[st.tone])}>{r.status === "running" ? <Spinner size={11} /> : st.label}</span>
          <span className="ml-auto shrink-0 text-[11.5px] text-faint">{TRIGGER_LABEL[r.trigger] ?? r.trigger}</span>
        </div>
        <div className="mt-0.5 text-[11.5px] text-faint" title={r.model ? `模型 ${r.model}` : undefined}>
          {runTimeline(r, now)}
          {r.thread_id ? " · 点开查看会话" : ""}
        </div>
        {r.summary && <div className={clsx("mt-0.5 line-clamp-2 text-[12px]", r.unread ? "text-fg" : "text-faint")}>{r.summary}</div>}
        {wt && (
          <div className="mt-1.5 rounded-lg border border-line bg-bg px-2.5 py-2 text-[12px]" onClick={stop}>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <GitBranch size={12} className="text-faint" />
              <span className="mono truncate text-muted" title={wt.path}>{wt.branch}</span>
              {r.status !== "running" && <span className="text-faint">{worktreeChangeText(wt)}</span>}
              <span className={clsx("text-faint", wt.state === "merged" && "text-ok")}>{worktreeStateLabel(wt)}</span>
              {pending && (
                <span className="ml-auto flex gap-1">
                  <Tip label="把这次的改动合并进主目录当前分支（保留提交历史），然后删掉 worktree">
                    <Button size="sm" variant="outline" disabled={!!busy} onClick={() => go("merge")}>{busy === "merge" ? <Spinner size={11} /> : <GitMerge size={12} />} 合并</Button>
                  </Tip>
                  <Tip label="改动压成一份放进主目录暂存区，由你自己提交">
                    <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => go("squash")}>{busy === "squash" ? <Spinner size={11} /> : <Layers size={12} />} 压成一份</Button>
                  </Tip>
                  <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => go("discard")}>{busy === "discard" ? <Spinner size={11} /> : <Trash2 size={12} />} 丢弃</Button>
                </span>
              )}
            </div>
            {pending && !!wt.files?.length && (
              <div className="mt-1 space-y-0.5">
                {wt.files.slice(0, 8).map((f) => (
                  <div key={f.path} className="mono flex gap-2 text-[11.5px]">
                    <span className="min-w-0 flex-1 truncate text-muted">{f.path}</span>
                    <span className="text-ok">+{f.additions}</span>
                    <span className="text-danger">−{f.deletions}</span>
                  </div>
                ))}
                {(wt.file_count ?? wt.files.length) > 8 && <div className="text-[11.5px] text-faint">还有 {(wt.file_count ?? wt.files.length) - 8} 个文件，点开会话或在 Worktree 对话框里查看</div>}
              </div>
            )}
            {wt.error && <div className="mt-1 text-[11.5px] text-warn">{wt.error}</div>}
          </div>
        )}
        {!!r.log && r.log.length > (r.summary?.length ?? 0) && (
          <div onClick={stop}>
            <button className="mt-1 flex items-center gap-1 text-[11.5px] text-faint hover:text-fg" onClick={() => setShowLog(!showLog)}>
              <FileText size={11} /> {showLog ? "收起日志" : "查看完整日志"}
            </button>
            {showLog && <pre className="mono mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-bg p-2 text-[11.5px] text-muted">{r.log}</pre>}
          </div>
        )}
      </div>
    </div>
  );
}

function EditDialog() {
  const editing = useAutomations((s) => s.editing);
  const set = useAutomations((s) => s.set);
  const save = useAutomations((s) => s.save);
  const workspaces = useApp((s) => s.workspaces);
  const models = useApp((s) => s.models);
  const rpc = useApp((s) => s.rpc);
  const [d, setD] = useState<Partial<Automation> | null>(null);
  const [preview, setPreview] = useState<{ ok: boolean; error: string; next_runs: number[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [timeErr, setTimeErr] = useState(false); // 每天的时间输入框里是不合法的值（不会被保存）
  useEffect(() => {
    setD(editing ? { ...editing } : null);
    setPreview(null); // 不沿用上一次打开时的预览
    setTimeErr(false);
  }, [editing]);
  const sched = d?.schedule;
  useEffect(() => {
    if (!sched || !rpc) return;
    let live = true; // 调度又变了 / 弹窗关了：丢弃旧请求晚到的结果
    const t = setTimeout(() => {
      rpc.call("automations/preview", { schedule: sched }).then((r) => live && setPreview(r)).catch(() => live && setPreview(null));
    }, 250);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [JSON.stringify(sched), rpc]);
  if (!d) return null;
  const up = (p: Partial<Automation>) => setD({ ...d, ...p });
  const kind = d.schedule?.kind ?? "daily";
  const setKind = (k: Schedule["kind"]) => {
    if (k === d.schedule?.kind) return; // 点当前类型不重置（否则时间框还显示旧文字，实际已被改回 09:00）
    setTimeErr(false);
    up({ schedule: k === "interval" ? { kind: "interval", minutes: 60 } : k === "daily" ? { kind: "daily", time: "09:00", weekdays: [] } : { kind: "cron", expr: "0 9 * * 1-5" } });
  };
  const daily = d.schedule?.kind === "daily" ? d.schedule : null;
  const wsOptions = workspaces.map((w) => ({ value: w.path, label: w.name, hint: w.path }));
  if (d.cwd && !workspaces.some((w) => w.path === d.cwd)) wsOptions.unshift({ value: d.cwd, label: base(d.cwd), hint: d.cwd });

  return (
    <Dialog.Root open onOpenChange={(o) => !o && set({ editing: null })}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 fadein" />
        <Dialog.Content aria-describedby={undefined} className="fixed left-1/2 top-[6%] z-50 flex max-h-[88vh] w-[600px] max-w-[94vw] -translate-x-1/2 flex-col rounded-2xl border border-line bg-elev p-4 shadow-[var(--shadow)] fadein">
          <div className="mb-3 flex items-center gap-2">
            <Clock size={15} className="text-muted" />
            <Dialog.Title className="flex-1 text-[14px] font-semibold text-fg">{d.id ? "编辑自动化" : "新建自动化"}</Dialog.Title>
            <Dialog.Close asChild><Button size="icon-sm" aria-label="关闭"><X size={14} /></Button></Dialog.Close>
          </div>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
            <Field label="名字"><Input value={d.name ?? ""} placeholder="例如：每日依赖检查" onChange={(e) => up({ name: e.target.value })} /></Field>
            <Field label="项目">
              <Select side="bottom" className="h-8 w-full max-w-none border border-line" value={d.cwd ?? ""} options={wsOptions} onChange={(v) => up({ cwd: v })} renderLabel={(o) => o?.label ?? "选择项目"} />
            </Field>
            <Field label="提示词" hint="每次运行都会把这段话作为新对话的第一条消息">
              <textarea
                className="h-28 w-full resize-y rounded-xl border border-line bg-bg px-3 py-2 text-[13px] text-fg outline-none focus:border-accent"
                value={d.prompt ?? ""}
                placeholder="检查项目依赖有没有安全更新，列出需要升级的包和原因。"
                onChange={(e) => up({ prompt: e.target.value })}
              />
            </Field>
            <Field label="什么时候运行">
              <div className="mb-2 inline-flex rounded-lg border border-line bg-soft p-0.5">
                {(["interval", "daily", "cron"] as const).map((k) => (
                  <button key={k} onClick={() => setKind(k)} className={clsx("h-7 rounded-md px-3 text-[12.5px]", kind === k ? "bg-elev text-fg shadow-sm" : "text-muted hover:text-fg")}>
                    {k === "interval" ? "每隔" : k === "daily" ? "每天" : "cron"}
                  </button>
                ))}
              </div>
              {d.schedule?.kind === "interval" && (
                <div className="flex items-center gap-2 text-[13px] text-muted">
                  每 <Input type="number" min={1} className="w-24" value={d.schedule.minutes} onChange={(e) => up({ schedule: { kind: "interval", minutes: Number(e.target.value) } })} /> 分钟
                </div>
              )}
              {daily && (
                <div className="space-y-2">
                  <Input className="w-28" defaultValue={daily.time} key={d.id ?? "new"} onChange={(e) => { const t = parseHHMM(e.target.value); setTimeErr(!t); if (t) up({ schedule: { ...daily, time: t } }); }} />
                  {timeErr && <div className="text-[11.5px] text-danger">时间要写成 HH:MM（00:00–23:59），例如 09:00</div>}
                  <div className="flex flex-wrap gap-1">
                    {[1, 2, 3, 4, 5, 6, 0].map((w) => {
                      const on = daily.weekdays.includes(w);
                      return (
                        <button key={w} onClick={() => up({ schedule: { ...daily, weekdays: on ? daily.weekdays.filter((x) => x !== w) : [...daily.weekdays, w] } })}
                          className={clsx("h-7 rounded-md border px-2.5 text-[12px]", on ? "border-accent bg-accent/10 text-fg" : "border-line text-muted hover:text-fg")}>
                          {WEEKDAYS[w]}
                        </button>
                      );
                    })}
                    <span className="self-center pl-1 text-[11.5px] text-faint">都不选 = 每天</span>
                  </div>
                </div>
              )}
              {d.schedule?.kind === "cron" && (
                <Input className="mono" value={d.schedule.expr} placeholder="分 时 日 月 星期，如 0 9 * * 1-5" onChange={(e) => up({ schedule: { kind: "cron", expr: e.target.value } })} />
              )}
              <div className={clsx("mt-1.5 text-[11.5px]", preview && !preview.ok ? "text-danger" : "text-faint")}>
                {preview ? (preview.ok ? `${describeSchedule(d.schedule)}，接下来：${preview.next_runs.map((t) => formatNextRun(t)).join("、")}（本机时间）` : preview.error) : ""}
              </div>
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="模型">
                <Select side="bottom" className="h-8 w-full max-w-none border border-line" value={d.model ?? ""} onChange={(v) => up({ model: v })}
                  options={[{ value: "", label: "默认模型" }, ...models.map((m) => ({ value: m.name, label: m.name }))]} />
              </Field>
              <Field label="权限模式">
                <Select side="bottom" className="h-8 w-full max-w-none border border-line" value={d.mode ?? "read-only"} options={MODES} onChange={(v) => up({ mode: v })} />
              </Field>
              <Field label="需要审批时" hint="无人值守，没人能点批准">
                <Select side="bottom" className="h-8 w-full max-w-none border border-line" value={d.approve ?? "deny"} onChange={(v) => up({ approve: v as "deny" | "auto" })}
                  options={[{ value: "deny", label: "一律拒绝（推荐）" }, { value: "auto", label: "自动批准（危险命令仍拒绝）" }]} />
              </Field>
              <Field label="超时（分钟）" hint="超过自动中断">
                <Input type="number" min={1} value={d.timeout_minutes ?? 60} onChange={(e) => up({ timeout_minutes: Number(e.target.value) })} />
              </Field>
            </div>
            <div className="flex items-center gap-3 rounded-xl border border-line px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="text-[13px] text-fg">在独立 worktree 中运行</div>
                <div className="text-[11.5px] text-faint">每次新建 ee/auto-名字-时间 分支的独立目录，改动不碰主目录；跑完在运行记录里看改动并合并或丢弃，没有改动的自动清理（仅 git 项目；否则在项目目录里跑）</div>
              </div>
              <Toggle on={!!d.worktree} onChange={(v) => up({ worktree: v })} />
            </div>
          </div>
          <div className="mt-3 flex items-center justify-end gap-2">
            <Button onClick={() => set({ editing: null })}>取消</Button>
            <Button variant="primary" disabled={busy || (!!daily && timeErr)} onClick={async () => { setBusy(true); await save(d); setBusy(false); }}>
              {busy && <Spinner size={12} />} {d.id ? "保存" : "创建"}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
