// 子代理卡片、网页搜索/抓取卡片、技能卡片（task / web_search / web_fetch / skill 工具条目的专用渲染）。
import clsx from "clsx";
import { Ban, BookOpen, Bot, Check, ChevronRight, Circle, Globe, Search, X } from "lucide-react";
import React, { useState } from "react";
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
  if (window.ee?.openExternal) window.ee.openExternal(url);
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

export function SubagentCard({ item }: { item: ToolItem }) {
  const rec: SubagentRecord | undefined = item.meta?.subagent;
  const running = item.status === "running" || rec?.status === "running";
  const [open, setOpen] = useState(false);
  const desc = rec?.description || String(item.args?.description ?? "子任务");
  const agent = rec?.agent || String(item.args?.agent ?? "general");
  const status = rec?.status ?? (item.status === "running" ? "running" : item.status === "ok" ? "completed" : item.status);
  const failed = !running && !["completed", "max_steps"].includes(status);
  const tools = rec?.tools ?? [];
  const isOpen = open || (running && tools.length > 0);
  return (
    <div className="fadein">
      <Head
        icon={<Bot size={14} />}
        title={
          <>
            {running ? "子代理正在" : "子代理"}
            <span className="ml-1 text-fg/90">{desc}</span>
          </>
        }
        right={
          <span className={clsx("shrink-0 text-[12px]", failed ? "text-danger" : "text-faint")}>
            {agent}
            {rec?.mode === "read-only" ? " · 只读" : ""} · {SUB_STATUS[status] ?? status}
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
          {!running && (rec?.summary || item.output) && (
            <div className="rounded-xl border border-line bg-soft px-3 py-2 text-[13px]">
              <div className="mb-1 text-[12px] text-faint">子代理的报告（交给主代理）</div>
              <Markdown text={rec?.summary || item.output} />
            </div>
          )}
          {rec?.model && <div className="text-[11.5px] text-faint">模型 {rec.model}</div>}
        </div>
      )}
    </div>
  );
}

type Hit = { title: string; url: string; snippet?: string; date?: string; site?: string };

export function WebSearchCard({ item }: { item: ToolItem }) {
  const running = item.status === "running";
  const [open, setOpen] = useState(false);
  const failed = item.status === "error" || item.status === "denied";
  const q = String(item.args?.query ?? item.meta?.query ?? "");
  const hits: Hit[] = item.meta?.results ?? [];
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
  const url = String(item.meta?.url ?? item.args?.url ?? "");
  const title = item.meta?.title as string | undefined;
  return (
    <div className="fadein">
      <Head
        icon={<Globe size={14} />}
        title={
          <>
            {running ? "正在打开" : failed ? "打开失败" : "打开了"} <span className="text-fg/90">{title || host(url)}</span>
          </>
        }
        right={!running && !failed && item.meta?.chars ? <span className="shrink-0 text-[12px] text-faint">{item.meta.chars} 字</span> : undefined}
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
  const name = String(item.meta?.name ?? item.args?.name ?? "");
  const path = item.meta?.path as string | undefined;
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
            <button className="mono max-w-full truncate text-[12px] text-accent hover:underline" onClick={() => window.ee?.openPath(path)} title={path}>
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
