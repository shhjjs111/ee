// D4-2：/review 选择审查对象的弹窗、/commit 提交弹窗、审查结果卡片。
import * as Dialog from "@radix-ui/react-dialog";
import clsx from "clsx";
import { CheckCircle2, GitCommitHorizontal, ListChecks, Play, RefreshCw, SearchCheck, TriangleAlert, Wrench, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { location, fixMessage, type ReviewResult } from "../lib/review";
import { useApp, type ReviewParams } from "../lib/store";
import type { ToolItem } from "../types";
import { PathLink } from "./Items";
import { Markdown } from "./Markdown";
import { Button, Select, Spinner } from "./ui";

function useCwd(): string {
  return useApp((s) => (s.activeThreadId ? s.details[s.activeThreadId]?.cwd : null) ?? s.activeWorkspace ?? "");
}

function Shell({ title, icon, children, onClose }: { title: string; icon: React.ReactNode; children: React.ReactNode; onClose: () => void }) {
  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 fadein" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-[14%] z-50 w-[520px] max-w-[92vw] -translate-x-1/2 rounded-2xl border border-line bg-elev p-4 shadow-[var(--shadow)] fadein"
        >
          <div className="mb-3 flex items-center gap-2">
            <span className="text-muted">{icon}</span>
            <Dialog.Title className="flex-1 text-[14px] font-semibold text-fg">{title}</Dialog.Title>
            <Dialog.Close asChild>
              <Button size="icon-sm" aria-label="关闭">
                <X size={14} />
              </Button>
            </Dialog.Close>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

type Targets = {
  is_repo: boolean;
  git_available?: boolean;
  branch?: string;
  dirty?: boolean;
  branches?: string[];
  base?: string;
  commits?: { sha: string; subject: string; when: string }[];
};

export function ReviewDialog({ initial }: { initial?: string }) {
  const s = useApp();
  const cwd = useCwd();
  const [tg, setTg] = useState<Targets | null>(null);
  const [kind, setKind] = useState<ReviewParams["kind"]>("uncommitted");
  const [branch, setBranch] = useState("");
  const [commit, setCommit] = useState("");
  const [text, setText] = useState(initial ?? "");
  const [busy, setBusy] = useState(false);
  const close = () => s.set({ dialog: null });

  useEffect(() => {
    if (!cwd || !s.rpc) return;
    s.rpc
      .call("review/targets", { cwd })
      .then((t: Targets) => {
        setTg(t);
        if (!t.is_repo) setKind("custom");
        else if (!t.dirty) setKind(t.base ? "branch" : "commit");
        const others = (t.branches ?? []).filter((b) => b !== t.branch);
        setBranch(t.base || others[0] || "");
        setCommit(t.commits?.[0]?.sha ?? "");
      })
      .catch((e) => {
        s.showToast((e as Error).message, "error");
        setTg({ is_repo: false });
        setKind("custom");
      });
  }, [cwd, s.rpc]);

  const others = (tg?.branches ?? []).filter((b) => b !== tg?.branch);
  const options = [
    { value: "uncommitted", label: "未提交的改动", hint: "工作区里还没提交的修改和新文件", disabled: !tg?.is_repo || !tg?.dirty },
    { value: "branch", label: "相对某个分支", hint: tg?.branch ? `当前分支 ${tg.branch} 自分叉以来的全部改动` : "", disabled: !tg?.is_repo || others.length === 0 },
    { value: "commit", label: "某一个提交", hint: "审查一个已经提交的改动", disabled: !tg?.is_repo || !tg?.commits?.length },
    { value: "custom", label: "按我的要求", hint: "不给 diff，写明要审查什么" },
  ];
  const canStart = !busy && tg !== null && (kind !== "custom" || text.trim().length > 0) && (kind !== "branch" || !!branch) && (kind !== "commit" || !!commit);

  const start = async () => {
    if (!canStart) return;
    setBusy(true);
    const ok = await s.startReview({ kind, ref: kind === "branch" ? branch : kind === "commit" ? commit : "", instructions: text.trim() });
    setBusy(false);
    if (ok) close();
  };

  return (
    <Shell title="代码审查" icon={<SearchCheck size={16} />} onClose={close}>
      {!tg ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-3 text-[13px]">
          <div className="space-y-1">
            {options.map((o) => (
              <label
                key={o.value}
                className={clsx(
                  "flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2",
                  kind === o.value ? "border-line-strong bg-soft" : "border-transparent hover:bg-hover",
                  o.disabled && "pointer-events-none opacity-40",
                )}
              >
                <input type="radio" className="mt-1" checked={kind === o.value} disabled={o.disabled} onChange={() => setKind(o.value as ReviewParams["kind"])} />
                <span className="min-w-0 flex-1">
                  <span className="block text-fg">{o.label}</span>
                  {o.hint && <span className="block text-[11.5px] text-faint">{o.hint}</span>}
                  {o.value === "branch" && kind === "branch" && others.length > 0 && (
                    <Select value={branch} side="bottom" className="mt-1 -ml-2" onChange={setBranch} options={others.map((b) => ({ value: b, label: b }))} />
                  )}
                  {o.value === "commit" && kind === "commit" && (
                    <Select
                      value={commit}
                      side="bottom"
                      className="mt-1 -ml-2 max-w-[400px]"
                      onChange={setCommit}
                      options={(tg.commits ?? []).map((c) => ({ value: c.sha, label: `${c.sha} ${c.subject}`, hint: c.when }))}
                    />
                  )}
                </span>
              </label>
            ))}
          </div>
          {!tg.is_repo && <div className="text-[12px] text-faint">{tg.git_available === false ? "没有找到 git，" : "这个文件夹不是 git 仓库，"}只能按要求审查。</div>}
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) start();
            }}
            rows={3}
            placeholder={kind === "custom" ? "要审查什么（必填），例如：检查 src/auth 里的登录流程有没有安全问题" : "额外要求（可选），例如：重点看并发和错误处理"}
            className="block w-full resize-none rounded-xl border border-line bg-bg px-3 py-2 text-[13px] leading-relaxed text-fg outline-none focus:border-line-strong"
          />
          <div className="flex items-center gap-2">
            <span className="flex-1 text-[11.5px] text-faint">审查回合只读，不会改文件；审完可以一键让它修</span>
            <Button variant="ghost" onClick={close}>
              取消
            </Button>
            <Button variant="primary" disabled={!canStart} onClick={start}>
              {busy ? <Spinner size={12} /> : "开始审查"}
            </Button>
          </div>
        </div>
      )}
    </Shell>
  );
}

export function CommitDialog() {
  const s = useApp();
  const cwd = useCwd();
  const [msg, setMsg] = useState("");
  const [info, setInfo] = useState<{ stage_all: boolean; files: string[] } | null>(null);
  const [gen, setGen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const close = () => s.set({ dialog: null });

  const generate = async () => {
    if (!cwd || !s.rpc) return;
    setGen(true);
    setErr("");
    try {
      const r = await s.rpc.call(
        "git/commit_message",
        { cwd, thread_id: s.activeThreadId ?? undefined, model: s.draftModel ?? undefined },
        120000,
      );
      setMsg(r.message);
      setInfo({ stage_all: r.stage_all, files: r.files });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setGen(false);
    }
  };

  useEffect(() => {
    generate();
  }, [cwd]);

  const commit = async () => {
    if (!msg.trim() || busy || !info) return;
    setBusy(true);
    setErr("");
    try {
      const r = await s.rpc!.call("git/commit", { cwd, message: msg, stage_all: info.stage_all }, 90000);
      s.showToast(`已提交 ${r.sha}`);
      useApp.setState((st) => ({ reviewNonce: st.reviewNonce + 1 }));
      close();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell title="提交改动" icon={<GitCommitHorizontal size={16} />} onClose={close}>
      <div className="space-y-3 text-[13px]">
        {info && (
          <div className="text-[12px] text-muted">
            {info.stage_all ? "没有暂存的改动，将暂存并提交全部改动" : "只提交已暂存的改动"}（{info.files.length} 个文件）
            <div className="mono mt-1 max-h-[88px] overflow-y-auto text-[11.5px] text-faint">
              {info.files.map((f) => (
                <div key={f} className="truncate">
                  {f}
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="relative">
          <textarea
            value={msg}
            onChange={(e) => setMsg(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) commit();
            }}
            rows={7}
            disabled={gen}
            placeholder={gen ? "正在根据改动写提交信息…" : "提交信息"}
            className="mono block w-full resize-none rounded-xl border border-line bg-bg px-3 py-2 text-[12.5px] leading-relaxed text-fg outline-none focus:border-line-strong"
          />
          {gen && (
            <div className="absolute right-3 top-2.5">
              <Spinner size={13} />
            </div>
          )}
        </div>
        {err && <div className="whitespace-pre-wrap break-words rounded-lg bg-del-bg px-3 py-2 text-[12px] text-danger">{err}</div>}
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={generate} disabled={gen || busy}>
            <RefreshCw size={13} />
            重新生成
          </Button>
          <span className="flex-1" />
          <Button variant="ghost" onClick={close}>
            取消
          </Button>
          <Button variant="primary" disabled={!msg.trim() || gen || busy || !info} onClick={commit}>
            {busy ? <Spinner size={12} /> : "提交"}
          </Button>
        </div>
      </div>
    </Shell>
  );
}

export function Dialogs() {
  const dialog = useApp((s) => s.dialog);
  if (!dialog) return null;
  return dialog.kind === "review" ? <ReviewDialog initial={dialog.instructions} /> : <CommitDialog />;
}

const PRIO = ["bg-del-bg text-danger", "bg-del-bg text-danger", "bg-soft text-fg", "bg-soft text-muted"];

export function ReviewCard({ result }: { result: ReviewResult }) {
  const s = useApp();
  const [picked, setPicked] = useState<Set<number>>(() => new Set(result.findings.map((f, i) => (f.priority <= 2 ? i : -1)).filter((i) => i >= 0)));
  const [open, setOpen] = useState<Set<number>>(new Set());
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const idle = !d || d.status === "idle";
  const chosen = useMemo(() => result.findings.filter((_, i) => picked.has(i)), [picked, result.findings]);
  const ok = result.overall === "correct";

  return (
    <div className="mt-2 overflow-hidden rounded-xl border border-line">
      <div className="flex items-center gap-2 border-b border-line bg-soft px-3 py-2 text-[12.5px]">
        {ok ? <CheckCircle2 size={14} className="text-add" /> : <TriangleAlert size={14} className="text-danger" />}
        <span className="font-medium text-fg">{ok ? "可以合入" : "有需要先修的问题"}</span>
        <span className="truncate text-muted">{result.summary}</span>
        <span className="flex-1" />
        <span className="shrink-0 text-faint">{result.findings.length ? `${result.findings.length} 个问题` : "没有发现问题"}</span>
      </div>
      {result.findings.map((f, i) => (
        <div key={i} className="border-b border-line px-3 py-2 last:border-b-0">
          <div className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-1"
              checked={picked.has(i)}
              onChange={() =>
                setPicked((cur) => {
                  const n = new Set(cur);
                  n.has(i) ? n.delete(i) : n.add(i);
                  return n;
                })
              }
            />
            <span className={clsx("mono mt-px shrink-0 rounded px-1.5 text-[11px] font-semibold", PRIO[f.priority])}>P{f.priority}</span>
            <div className="min-w-0 flex-1">
              <button
                className="block w-full text-left text-[13px] text-fg hover:underline"
                onClick={() =>
                  setOpen((cur) => {
                    const n = new Set(cur);
                    n.has(i) ? n.delete(i) : n.add(i);
                    return n;
                  })
                }
              >
                {f.title}
              </button>
              {f.file && <PathLink path={f.file} label={location(f)} className="text-[11.5px] text-faint" />}
              {open.has(i) && f.body && <div className="mt-1 whitespace-pre-wrap text-[12.5px] leading-relaxed text-muted">{f.body}</div>}
            </div>
          </div>
        </div>
      ))}
      {result.findings.length > 0 && (
        <div className="flex items-center gap-2 border-t border-line px-3 py-2">
          <span className="flex-1 text-[11.5px] text-faint">选中要修的问题（默认 P0–P2），按当前权限模式修改</span>
          <Button size="sm" variant="soft" disabled={!chosen.length || !idle} onClick={() => s.send(fixMessage(chosen))}>
            <Wrench size={13} />
            修复选中的 {chosen.length} 个
          </Button>
        </div>
      )}
    </div>
  );
}

/** 计划模式提交的方案（present_plan）：批准后切模式并让模型按方案执行。 */
export function PlanProposalCard({ item }: { item: ToolItem }) {
  const s = useApp();
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const idle = !d || d.status === "idle";
  const idx = d ? d.items.findIndex((x) => x.id === item.id) : -1;
  const later = d && idx >= 0 ? d.items.slice(idx + 1).some((x) => x.type === "user") : false;
  const plan = String(item.details?.plan ?? "");
  const title = String(item.details?.title || "实施方案");

  const run = (mode: "default" | "auto") => {
    localStorage.setItem("ee.mode", mode);
    s.set({ draftMode: mode });
    s.send("方案已批准，请按上面的方案开始执行；每完成一步更新任务清单，最后跑验证并汇报结果。");
  };

  return (
    <div className="fadein overflow-hidden rounded-xl border border-line-strong">
      <div className="flex items-center gap-2 border-b border-line bg-soft px-3 py-2 text-[12.5px]">
        <ListChecks size={14} className="text-muted" />
        <span className="font-medium text-fg">{title}</span>
        <span className="flex-1" />
        <span className="text-faint">{later ? "已处理" : "等你批准"}</span>
      </div>
      <div className="max-h-[480px] overflow-y-auto px-4 py-2">
        <Markdown text={plan} />
      </div>
      {!later && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-2">
          <span className="flex-1 text-[11.5px] text-faint">不满意就直接在输入框写修改意见，ee 会改方案再提交</span>
          <Button size="sm" variant="soft" disabled={!idle} onClick={() => run("default")}>
            <Play size={12} />
            按此执行（默认模式）
          </Button>
          <Button size="sm" variant="primary" disabled={!idle} onClick={() => run("auto")}>
            <Play size={12} />
            按此执行（全自动）
          </Button>
        </div>
      )}
    </div>
  );
}
