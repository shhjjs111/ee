import clsx from "clsx";
import {
  AlertTriangle,
  Ban,
  Brain,
  Check,
  ChevronRight,
  Circle,
  CircleDot,
  FilePen,
  FilePlus2,
  FileSearch,
  FolderSearch,
  ListChecks,
  ShieldAlert,
  ShieldCheck,
  SquareTerminal,
  Wrench,
  X,
} from "lucide-react";
import React, { useState } from "react";
import { diffStat, formatDuration, shortPath } from "../lib/reduce";
import { useApp } from "../lib/store";
import type { ApprovalItem, AssistantItem, Item, PlanItem, ToolItem, TurnEndItem } from "../types";
import { DiffView, Stat } from "./DiffView";
import { Markdown } from "./Markdown";
import { Button, Spinner, Tip } from "./ui";

// ------------------------------------------------------------ 通用折叠行
function Row({
  icon,
  title,
  right,
  open,
  onToggle,
  children,
  active,
  tone,
}: {
  icon: React.ReactNode;
  title: React.ReactNode;
  right?: React.ReactNode;
  open: boolean;
  onToggle: () => void;
  children?: React.ReactNode;
  active?: boolean;
  tone?: "error" | "muted";
}) {
  return (
    <div className="fadein">
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
        {children !== undefined && (
          <ChevronRight
            size={14}
            className={clsx("shrink-0 opacity-0 transition-all group-hover:opacity-70", open && "rotate-90 opacity-70")}
          />
        )}
      </button>
      {open && children !== undefined && <div className="mb-2 ml-6 mt-1">{children}</div>}
    </div>
  );
}

function Mono({ children }: { children: React.ReactNode }) {
  return <code className="mono rounded bg-soft px-1.5 py-px text-[12px] text-fg">{children}</code>;
}

function OutputBox({ text }: { text: string }) {
  const t = (text || "").replace(/^\s*\n/, "").trimEnd();
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-code">
      <pre className="mono max-h-[320px] overflow-auto whitespace-pre-wrap break-all px-3 py-2.5 text-[12px] leading-[1.55] text-fg">
        {t || "（无输出）"}
      </pre>
    </div>
  );
}

function Approved({ how }: { how?: string }) {
  if (how !== "once" && how !== "always") return null;
  return (
    <Tip label={how === "always" ? "你批准了（本会话内同类操作不再询问）" : "你批准了这一次"}>
      <ShieldCheck size={13} className="shrink-0 text-faint" />
    </Tip>
  );
}

// ------------------------------------------------------------ 用户 / 助手
export function UserMessage({ text, queued }: { text: string; queued?: boolean }) {
  return (
    <div className="fadein flex justify-end">
      <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-2xl bg-soft px-4 py-2.5 text-[14px] leading-relaxed text-fg">
        {text}
        {queued && <div className="mt-1 text-[11.5px] text-faint">已排队，当前步骤结束后送入</div>}
      </div>
    </div>
  );
}

export function AssistantMessage({ item }: { item: AssistantItem }) {
  const [open, setOpen] = useState(false);
  const thinkingOnly = !item.text && !!item.reasoning;
  return (
    <div className="fadein space-y-1">
      {item.reasoning && (
        <Row
          icon={<Brain size={14} />}
          title={item.streaming && thinkingOnly ? "思考中…" : "已思考"}
          active={item.streaming && thinkingOnly}
          open={open}
          onToggle={() => setOpen(!open)}
        >
          <div className="whitespace-pre-wrap border-l-2 border-line pl-3 text-[12.5px] leading-relaxed text-muted">
            {item.reasoning}
          </div>
        </Row>
      )}
      {item.text && (
        <div className={clsx(item.streaming && "stream-caret-wrap")}>
          <Markdown text={item.text} />
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------ 工具
function exploreLabel(it: ToolItem): { verb: string; target: string; icon: React.ReactNode } {
  const a = it.args || {};
  if (it.name === "read") {
    const k = it.meta?.kind;
    return { verb: k === "list" ? "列出" : "读取", target: shortPath(String(a.path ?? ""), 3), icon: <FileSearch size={13} /> };
  }
  if (it.name === "grep") return { verb: "搜索", target: String(a.pattern ?? ""), icon: <FolderSearch size={13} /> };
  if (it.name === "glob") return { verb: "查找", target: String(a.pattern ?? ""), icon: <FolderSearch size={13} /> };
  return { verb: it.name, target: it.summary, icon: <Wrench size={13} /> };
}

export function ExploreGroup({ items }: { items: Item[] }) {
  const [open, setOpen] = useState(false);
  const tools = items as ToolItem[];
  const running = tools.some((t) => t.status === "running");
  const reads = tools.filter((t) => t.name === "read" && t.meta?.kind !== "list").length;
  const searches = tools.length - reads;
  const parts: string[] = [];
  if (reads) parts.push(`${reads} 个文件`);
  if (searches) parts.push(`${searches} 次搜索`);
  return (
    <Row
      icon={<FileSearch size={14} />}
      title={`${running ? "正在浏览" : "已浏览"} ${parts.join("，")}`}
      active={running}
      open={open}
      onToggle={() => setOpen(!open)}
    >
      <div className="space-y-0.5 border-l-2 border-line pl-3">
        {tools.map((t) => {
          const l = exploreLabel(t);
          return (
            <div key={t.id} className="flex min-w-0 items-center gap-2 text-[12.5px] text-muted">
              <span className="text-faint">{l.icon}</span>
              <span className="shrink-0">{l.verb}</span>
              <span className="mono truncate text-fg/80">{l.target}</span>
              {t.status === "error" && <span className="text-danger">失败</span>}
            </div>
          );
        })}
      </div>
    </Row>
  );
}

export function ToolCard({ item, approval }: { item: ToolItem; approval?: string }) {
  const running = item.status === "running";
  const [open, setOpen] = useState(false);
  const failed = item.status === "error";
  const denied = item.status === "denied";
  const a = item.args || {};

  if (item.name === "shell") {
    const cmd = String(a.command ?? item.meta?.command ?? "");
    const exit = item.meta?.exit as number | undefined;
    const isOpen = open || (running && !!item.output);
    return (
      <Row
        icon={denied ? <Ban size={14} /> : <SquareTerminal size={14} />}
        title={
          <>
            {running ? "正在运行" : denied ? "未运行" : "运行了"} <Mono>{cmd.split("\n")[0].slice(0, 120)}</Mono>
          </>
        }
        right={
          <>
            <Approved how={approval} />
            {!running && exit !== undefined && exit !== null && exit !== 0 && (
              <span className="shrink-0 text-[12px] text-danger">退出码 {exit}</span>
            )}
          </>
        }
        active={running}
        open={isOpen}
        onToggle={() => setOpen(!isOpen)}
      >
        <OutputBox text={item.output || ""} />
      </Row>
    );
  }

  if (item.meta?.kind === "edit" || ["write", "edit", "apply_patch"].includes(item.name)) {
    const files: { verb: string; path: string; added: number; removed: number }[] =
      item.meta?.files ?? (item.meta?.path ? [{ verb: item.meta.verb, path: item.meta.path, added: item.meta.added, removed: item.meta.removed }] : []);
    const st = item.diff ? diffStat(item.diff) : { add: item.meta?.added ?? 0, del: item.meta?.removed ?? 0 };
    const path = files[0]?.path ?? String(a.path ?? "");
    const created = files[0]?.verb === "Created" || files[0]?.verb === "Added";
    const verb = running ? "正在编辑" : denied ? "未修改" : failed ? "编辑失败" : created ? "已创建" : "已编辑";
    return (
      <Row
        icon={created ? <FilePlus2 size={14} /> : <FilePen size={14} />}
        title={
          <>
            {verb} <span className="mono text-fg/90">{files.length > 1 ? `${files.length} 个文件` : shortPath(path, 3)}</span>
          </>
        }
        right={
          <>
            {!running && !failed && !denied && <Stat add={st.add} del={st.del} className="shrink-0" />}
            <Approved how={approval} />
          </>
        }
        active={running}
        tone={failed ? "error" : undefined}
        open={open}
        onToggle={() => setOpen(!open)}
      >
        {item.diff ? (
          <div className="overflow-hidden rounded-xl border border-line bg-code">
            <DiffView patch={item.diff} maxHeight={360} />
          </div>
        ) : (
          <OutputBox text={item.output} />
        )}
      </Row>
    );
  }

  if (item.name === "todo") return null; // 计划单独渲染

  return (
    <Row
      icon={<Wrench size={14} />}
      title={
        <>
          {running ? "正在使用" : "使用了"} <Mono>{item.name}</Mono>
        </>
      }
      active={running}
      tone={failed ? "error" : undefined}
      open={open}
      onToggle={() => setOpen(!open)}
    >
      <OutputBox text={item.output} />
    </Row>
  );
}

// ------------------------------------------------------------ 计划
export function PlanCard({ item }: { item: PlanItem }) {
  const done = item.items.filter((x) => x.status === "completed").length;
  return (
    <div className="fadein rounded-xl border border-line bg-elev px-4 py-3">
      <div className="mb-2 flex items-center gap-2 text-[12.5px] font-medium text-muted">
        <ListChecks size={14} /> 计划
        <span className="ml-auto text-faint">
          {done}/{item.items.length}
        </span>
      </div>
      <ul className="space-y-1.5">
        {item.items.map((x, i) => (
          <li key={i} className="flex items-start gap-2 text-[13px]">
            {x.status === "completed" ? (
              <span className="mt-[3px] flex h-[14px] w-[14px] shrink-0 items-center justify-center rounded-full bg-ok text-bg">
                <Check size={10} strokeWidth={3} />
              </span>
            ) : x.status === "in_progress" ? (
              <CircleDot size={14} className="mt-[3px] shrink-0 text-accent" />
            ) : (
              <Circle size={14} className="mt-[3px] shrink-0 text-faint" />
            )}
            <span className={clsx(x.status === "completed" && "text-faint line-through", x.status === "in_progress" && "font-medium text-fg")}>
              {x.content}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ------------------------------------------------------------ 审批
export function ApprovalCard({ item }: { item: ApprovalItem }) {
  const respond = useApp((s) => s.respond);
  const [reason, setReason] = useState("");
  const [askReason, setAskReason] = useState(false);
  const pending = item.status === "pending";
  const isCmd = item.tool === "shell";
  const isPatch = item.tool === "apply_patch";
  const label =
    item.status === "once" ? "已允许一次" : item.status === "always" ? "已允许（本会话内不再询问）" : item.status === "deny" ? "已拒绝" : "已取消";
  if (!pending) {
    return (
      <div className="fadein flex items-center gap-2 py-0.5 text-[12.5px] text-faint">
        {item.status === "deny" || item.status === "cancelled" ? <X size={13} /> : <ShieldCheck size={13} />}
        <span className="truncate">
          {label}：{item.title}
          {item.reason ? `（${item.reason}）` : ""}
        </span>
      </div>
    );
  }
  const always =
    isCmd && item.prefix ? `允许，本会话内 ${item.prefix} 开头的命令不再询问` : "允许，本会话内同类操作不再询问";
  return (
    <div className="fadein overflow-hidden rounded-xl border border-warn/40 bg-elev shadow-[var(--shadow)]">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5 text-[13px] font-medium">
        <ShieldAlert size={15} className="text-warn" />
        需要你的批准
      </div>
      <div className="px-4 py-3">
        <div className="mb-2 text-[13px] text-fg">{item.title.replace(/^运行命令：.*/s, "运行命令")}</div>
        {item.detail && (
          <div className="overflow-hidden rounded-lg border border-line bg-code">
            {isPatch || item.detail.startsWith("---") || item.detail.startsWith("@@") ? (
              <DiffView patch={item.detail} maxHeight={260} />
            ) : (
              <pre className="mono max-h-[220px] overflow-auto whitespace-pre-wrap break-all px-3 py-2 text-[12.5px] text-fg">
                {isCmd ? `$ ${item.detail}` : item.detail}
              </pre>
            )}
          </div>
        )}
        {askReason && (
          <input
            autoFocus
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) respond(item.call_id, "deny", reason);
            }}
            placeholder="告诉 ee 应该怎么做（可选），回车确认拒绝"
            className="mt-3 h-8 w-full rounded-lg border border-line bg-bg px-2.5 text-[13px] outline-none focus:border-accent"
          />
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-line bg-soft/60 px-4 py-2.5">
        <Button variant="primary" size="sm" onClick={() => respond(item.call_id, "once")}>
          允许一次
        </Button>
        <Button variant="outline" size="sm" onClick={() => respond(item.call_id, "always")} title={always}>
          本会话都允许
        </Button>
        <div className="flex-1" />
        {askReason ? (
          <Button variant="danger" size="sm" onClick={() => respond(item.call_id, "deny", reason)}>
            确认拒绝
          </Button>
        ) : (
          <>
            <Button variant="ghost" size="sm" onClick={() => setAskReason(true)}>
              拒绝并说明…
            </Button>
            <Button variant="danger" size="sm" onClick={() => respond(item.call_id, "deny")}>
              拒绝
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------ 其他
export function NoticeRow({ text, level }: { text: string; level: string }) {
  return (
    <div className={clsx("fadein flex items-start gap-2 text-[12.5px]", level === "warn" ? "text-warn" : "text-faint")}>
      <AlertTriangle size={13} className="mt-[3px] shrink-0" />
      <span className="whitespace-pre-wrap">{text}</span>
    </div>
  );
}

export function ErrorRow({ message }: { message: string }) {
  return (
    <div className="fadein flex items-start gap-2 rounded-xl border border-danger/30 bg-del-bg px-3.5 py-2.5 text-[13px] text-danger">
      <AlertTriangle size={14} className="mt-[3px] shrink-0" />
      <span className="whitespace-pre-wrap break-words">{message}</span>
    </div>
  );
}

export function TurnEnd({ item }: { item: TurnEndItem }) {
  const u = item.usage || {};
  const reason =
    item.reason === "interrupted" ? "已停止" : item.reason === "error" ? "出错" : item.reason === "max_steps" ? "达到步数上限" : "完成";
  const bits = [`${reason} · ${formatDuration(item.duration_ms)}`];
  if (u.hit_rate !== undefined && u.hit_rate !== null) bits.push(`缓存命中 ${Math.round(u.hit_rate * 100)}%`);
  if (u.cost !== undefined && u.cost !== null) bits.push(`累计 ${u.currency ?? ""}${u.cost.toFixed(4)}`);
  return (
    <div className="flex items-center gap-3 py-1 text-[11.5px] text-faint">
      <div className="h-px flex-1 bg-line" />
      <span>{bits.join(" · ")}</span>
      <div className="h-px flex-1 bg-line" />
    </div>
  );
}
