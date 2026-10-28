// 子代理卡片、网页搜索/抓取卡片、技能卡片（task / web_search / web_fetch / skill 工具条目的专用渲染）。
import { reportError } from "../lib/errors";
import { openPath } from "../lib/openPath";
import clsx from "clsx";
import { Ban, BookOpen, Bot, Check, ChevronRight, Circle, Clock, GitBranch, Globe, Search, X } from "lucide-react";
import React, { useState } from "react";
import { subagentStatus } from "../lib/reduce";
import type { SubagentRecord, ToolItem } from "../types";
import { Markdown } from "./Markdown";
import { Spinner } from "./ui";

function Head({
  icon,
  title,
  right,
  open,
  onToggle,
  active,
  tone,
}: {
  icon: React.ReactNode;
  title: React.ReactNode;
  right?: React.ReactNode;
  open: boolean;
  onToggle: () => void;
  active?: boolean;
  tone?: "error";
}) {
  return (
    <button
      onClick={onToggle}
      className={clsx(
        "group flex w-full min-w-0 items-center gap-2 rounded-lg py-1 text-left text-[13px] transition-colors",
        tone === "error" ? "text-danger" : "text-muted hover:text-fg",
      )}
    >
      <span className="flex h-4 w-4 shrink-0 items-center justify-center">{active ? <Spinner size={13} /> : icon}</span>
      <span className={clsx("min-w-0 truncate", active && "shimmer")}>{title}</span>
      {right}
      <ChevronRight size={14} className={clsx("shrink-0 opacity-0 transition-all group-hover:opacity-70", open && "rotate-90 opacity-70")} />
    </button>
  );
}

function openUrl(url: string) {
  if (window.ee?.openExternal) window.ee.openExternal(url).catch(reportError("打开链接失败"));
  else window.open(url, "_blank");
}

function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

const SUB_STATUS: Record<string, string> = {
  queued: "排队中",
  running: "进行中",
  completed: "已完成",
  max_steps: "步数用完",
  interrupted: "已中断",
  error: "出错",
  loop: "重复调用被停止",
};

function StepIcon({ status }: { status: string }) {
  if (status === "running") return <Spinner size={11} />;
  if (status === "ok") return <Check size={12} className="text-faint" />;
  if (status === "error") return <X size={12} className="text-danger" />;
  if (status === "interrupted") return <Ban size={12} className="text-faint" />;
  return <Circle size={10} className="text-faint" />;
}

function tokens(n: number): string {
  return n >= 10000 ? `${(n / 1000).toFixed(0)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function usageText(u?: Record<string, any>): string {
  if (!u || !u.requests) return "";
  const bits = [`${tokens((u.prompt ?? 0) + (u.completion ?? 0))} token`];
  if (u.cost !== undefined && u.cost !== null) bits.push(`${u.currency ?? ""}${Number(u.cost).toFixed(4)}`);
  return bits.join(" · ");
}

function WorktreeInfo({ wt, running }: { wt: NonNullable<SubagentRecord["worktree"]>; running: boolean }) {
  const files = wt.files ?? [];
  return (
    <div className="rounded-lg border border-line bg-soft px-2.5 py-1.5 text-[12px] text-muted">
      <div className="flex min-w-0 items-center gap-1.5">
        <GitBranch size={12} className="shrink-0 text-faint" />
        <span className="mono truncate text-fg/80" title={wt.path}>
          {wt.branch}
        </span>
        <span className="shrink-0 text-faint">
          {running
            ? "独立 worktree，改动不进主目录"
            : wt.removed
              ? "没有改动，已删除"
              : files.length > 0
                ? `${wt.file_count ?? files.length} 个文件 +${wt.additions ?? 0} -${wt.deletions ?? 0}`
                : `已保留 worktree（${wt.kept_reason || "没有统计到改动"}）`}
        </span>
      </div>
      {!running && !wt.removed && files.length > 0 && (
        <div className="mt-1 space-y-0.5 pl-[18px]">
          {files.slice(0, 12).map((f) => (
            <div key={f.path} className="flex min-w-0 gap-2">
              <span className="mono truncate">{f.path}</span>
              <span className="shrink-0 text-faint">{f.binary ? "二进制" : `+${f.additions} -${f.deletions}`}</span>
            </div>
          ))}
          {files.length > 12 && <div className="text-faint">…共 {files.length} 个文件</div>}
          <div className="text-faint">可在项目菜单「Worktree…」里查看、合并或删除</div>
        </div>
      )}
    </div>
  );
}

export function SubagentCard({ item }: { item: ToolItem }) {
  const rec: SubagentRecord | undefined = item.details?.subagent;
  const status = subagentStatus(item);
  const running = status === "running";
  const queued = status === "queued";
  const [open, setOpen] = useState(false);
  const desc = rec?.description || String(item.args?.description ?? "子任务");
  const agent = rec?.agent || String(item.args?.agent ?? "general");
  const failed = !running && !queued && !["completed", "max_steps"].includes(status);
  const tools = rec?.tools ?? [];
  const isOpen = open || (running && tools.length > 0);
  const usage = usageText(rec?.usage);
  return (
    <div className="fadein" data-testid="subagent-card">
      <Head
        icon={queued ? <Clock size={14} /> : <Bot size={14} />}
        title={
          <>
            {running ? "子代理正在" : queued ? "子代理等待" : "子代理"}
            <span className="ml-1 text-fg/90">{desc}</span>
          </>
        }
        right={
          <span className={clsx("shrink-0 text-[12px]", failed ? "text-danger" : "text-faint")}>
            {agent}
            {rec?.mode === "read-only" ? " · 只读" : ""}
            {rec?.fork || item.args?.fork === true ? (
              <span title="分叉子代理：继承主会话到这一步的完整上下文，只把结论交回主会话"> · 分叉</span>
            ) : null}
            {rec?.worktree ? " · worktree" : ""} · {SUB_STATUS[status] ?? status}
            {rec?.step ? ` · ${rec.step}${rec.max_steps ? `/${rec.max_steps}` : ""} 步` : ""}
          </span>
        }
        active={running}
        tone={failed && status === "error" ? "error" : undefined}
        open={isOpen}
        onToggle={() => setOpen(!isOpen)}
      />
      {isOpen && (
        <div className="mb-2 ml-6 mt-1 space-y-2 border-l-2 border-line pl-3">
          {rec?.worktree && <WorktreeInfo wt={rec.worktree} running={running || queued} />}
          {tools.length > 0 && (
            <div className="space-y-0.5">
              {tools.slice(-30).map((t) => (
                <div key={t.id} className="flex min-w-0 items-center gap-2 text-[12.5px] text-muted">
                  <span className="flex w-3 shrink-0 justify-center">
                    <StepIcon status={t.status} />
                  </span>
                  <span className="mono truncate text-fg/80">{t.summary || t.name}</span>
                </div>
              ))}
              {tools.length > 30 && <div className="text-[12px] text-faint">（只显示最近 30 步，共 {tools.length} 步）</div>}
            </div>
          )}
          {running && rec?.text && <div className="line-clamp-3 text-[12.5px] text-muted">{rec.text}</div>}
          {!running && !queued && (rec?.summary || item.output) && (
            <div className="rounded-xl border border-line bg-soft px-3 py-2 text-[13px]">
              <div className="mb-1 text-[12px] text-faint">子代理的报告（交给主代理）</div>
              <Markdown text={rec?.summary || item.output} />
            </div>
          )}
          {(rec?.model || usage) && (
            <div className="text-[11.5px] text-faint">
              {rec?.model ? `模型 ${rec.model}` : ""}
              {rec?.model && usage ? " · " : ""}
              {usage}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** 同一步并行派出的多个子代理：组头显示总数和各状态数，下面每个子代理一张卡片（D5-2）。 */
export function SubagentGroup({ items }: { items: ToolItem[] }) {
  const counts: Record<string, number> = {};
  for (const it of items) {
    const st = subagentStatus(it);
    counts[st] = (counts[st] ?? 0) + 1;
  }
  const active = (counts.running ?? 0) + (counts.queued ?? 0);
  const parts = Object.entries(counts).map(([k, n]) => `${n} 个${SUB_STATUS[k] ?? k}`);
  return (
    <div className="fadein" data-testid="subagent-group">
      <div className="flex items-center gap-2 py-0.5 text-[12.5px] text-faint">
        <Bot size={13} />
        <span className={clsx(active > 0 && "shimmer")}>
          {active > 0 ? `${items.length} 个子代理并行` : `${items.length} 个子代理`}
        </span>
        <span>· {parts.join("，")}</span>
      </div>
      <div className="space-y-0.5 border-l-2 border-line/60 pl-2">
        {items.map((it) => (
          <SubagentCard key={it.id} item={it} />
        ))}
      </div>
    </div>
  );
}

type Hit = { title: string; url: string; snippet?: string; date?: string; site?: string };

export function WebSearchCard({ item }: { item: ToolItem }) {
  const running = item.status === "running";
  const [open, setOpen] = useState(false);
  const failed = item.status === "error" || item.status === "denied";
  const q = String(item.args?.query ?? item.details?.query ?? "");
  const hits: Hit[] = item.details?.results ?? [];
  return (
    <div className="fadein">
      <Head
        icon={<Search size={14} />}
        title={
          <>
            {running ? "正在搜索" : failed ? "搜索失败" : "搜索了"} <span className="text-fg/90">{q}</span>
          </>
        }
        right={!running && !failed ? <span className="shrink-0 text-[12px] text-faint">{hits.length} 条结果</span> : undefined}
        active={running}
        tone={failed ? "error" : undefined}
        open={open}
        onToggle={() => setOpen(!open)}
      />
      {open && (
        <div className="mb-2 ml-6 mt-1 space-y-1.5">
          {failed || hits.length === 0 ? (
            <div className="whitespace-pre-wrap text-[12.5px] text-muted">{item.output || "（没有结果）"}</div>
          ) : (
            hits.map((h, i) => (
              <div key={i} className="min-w-0">
                <button className="flex max-w-full items-center gap-1.5 text-left text-[13px] text-fg hover:underline" onClick={() => openUrl(h.url)} title={h.url}>
                  <Globe size={12} className="shrink-0 text-faint" />
                  <span className="truncate">{h.title || h.url}</span>
                  <span className="shrink-0 text-[11.5px] text-faint">{h.site || host(h.url)}{h.date ? ` · ${h.date}` : ""}</span>
                </button>
                {h.snippet && <div className="line-clamp-2 pl-[18px] text-[12px] text-muted">{h.snippet}</div>}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export function WebFetchCard({ item }: { item: ToolItem }) {
  const running = item.status === "running";
  const [open, setOpen] = useState(false);
  const failed = item.status === "error" || item.status === "denied";
  const url = String(item.details?.url ?? item.args?.url ?? "");
  const title = item.details?.title as string | undefined;
  return (
    <div className="fadein">
      <Head
        icon={<Globe size={14} />}
        title={
          <>
            {running ? "正在打开" : failed ? "打开失败" : "打开了"} <span className="text-fg/90">{title || host(url)}</span>
          </>
        }
        right={!running && !failed && item.details?.chars ? <span className="shrink-0 text-[12px] text-faint">{item.details.chars} 字</span> : undefined}
        active={running}
        tone={failed ? "error" : undefined}
        open={open}
        onToggle={() => setOpen(!open)}
      />
      {open && (
        <div className="mb-2 ml-6 mt-1 space-y-1.5">
          <button className="mono max-w-full truncate text-[12px] text-accent hover:underline" onClick={() => openUrl(url)} title={url}>
            {url}
          </button>
          <pre className="mono max-h-[260px] overflow-auto whitespace-pre-wrap break-all rounded-xl border border-line bg-code px-3 py-2.5 text-[12px] leading-[1.55] text-fg">
            {(item.output || "").slice(0, 6000) || "（无输出）"}
          </pre>
        </div>
      )}
    </div>
  );
}

/** skill 工具（D5-5）：「加载了技能 xxx」，展开看 SKILL.md 全文和文件列表。 */
export function SkillCard({ item }: { item: ToolItem }) {
  const running = item.status === "running";
  const [open, setOpen] = useState(false);
  const failed = item.status === "error" || item.status === "denied";
  const name = String(item.details?.name ?? item.args?.name ?? "");
  const path = item.details?.path as string | undefined;
  return (
    <div className="fadein">
      <Head
        icon={<BookOpen size={14} />}
        title={
          <>
            {running ? "正在加载技能" : failed ? "加载技能失败" : "加载了技能"} <span className="mono text-fg/90">{name}</span>
          </>
        }
        active={running}
        tone={failed ? "error" : undefined}
        open={open}
        onToggle={() => setOpen(!open)}
      />
      {open && (
        <div className="mb-2 ml-6 mt-1 space-y-1.5">
          {path && (
            <button className="mono max-w-full truncate text-[12px] text-accent hover:underline" onClick={() => openPath(path)} title={path}>
              {path}
            </button>
          )}
          <pre className="mono max-h-[260px] overflow-auto whitespace-pre-wrap break-all rounded-xl border border-line bg-code px-3 py-2.5 text-[12px] leading-[1.55] text-fg">
            {(item.output || "").slice(0, 8000) || "（无输出）"}
          </pre>
        </div>
      )}
    </div>
  );
}
