import clsx from "clsx";
import {
  MessageCircleQuestion,
  SearchCheck,
  GitFork,
  Pencil,
  AlertTriangle,
  Ban,
  Bot,
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
  Plug,
} from "lucide-react";
import React, { useMemo, useState } from "react";
import { diffStat, formatDuration, shortPath } from "../lib/reduce";
import { splitReview } from "../lib/review";
import { PlanProposalCard, ReviewCard } from "./ReviewDialogs";
import { useApp } from "../lib/store";
import type { ApprovalItem, AssistantItem, Item, PlanItem, QuestionItem, ToolItem, TurnEndItem, UserItem } from "../types";
import { SkillCard, SubagentCard, WebFetchCard, WebSearchCard } from "./AgentItems";
import { DiffView, Stat } from "./DiffView";
import { Markdown } from "./Markdown";
import { Button, Spinner, Tip } from "./ui";
import { DELIVERED_LABEL } from "../lib/queue";

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

/** 可点击的文件路径：在右侧「文件」标签里打开。 */
export function PathLink({ path, label, className }: { path: string; label?: string; className?: string }) {
  const openFile = useApp((s) => s.openFile);
  if (!path) return <span className={className}>{label}</span>;
  return (
    <span
      role="link"
      tabIndex={0}
      title={`在文件面板中打开 ${path}`}
      className={clsx(
        "mono cursor-pointer rounded-sm decoration-faint/60 underline-offset-[3px] hover:text-fg hover:underline",
        className,
      )}
      onClick={(e) => {
        e.stopPropagation();
        openFile(path);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.stopPropagation();
          e.preventDefault();
          openFile(path);
        }
      }}
    >
      {label ?? path}
    </span>
  );
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
export function UserMessage({ item }: { item: UserItem }) {
  const s = useApp();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [busy, setBusy] = useState(false);
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const idle = !d || d.status === "idle";
  const canBranch = typeof item.hist === "number" && !item.queued && item.kind !== "review";

  if (editing) {
    const send = async () => {
      if (!draft.trim() || busy) return;
      setBusy(true);
      const ok = await s.editResend(item.id, draft.trim());
      setBusy(false);
      if (ok) setEditing(false);
    };
    return (
      <div className="fadein flex justify-end">
        <div className="w-[80%] rounded-2xl border border-line-strong bg-elev p-2">
          <textarea
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              } else if (e.key === "Escape") setEditing(false);
            }}
            rows={Math.min(10, Math.max(2, draft.split("\n").length))}
            className="block w-full resize-none bg-transparent px-2 py-1 text-[14px] leading-relaxed text-fg outline-none"
          />
          <div className="flex items-center gap-2 px-1 pt-1">
            <span className="flex-1 text-[11.5px] text-faint">重新发送会丢掉这条之后的对话，并恢复之后 ee 改过的文件</span>
            <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
              取消
            </Button>
            <Button size="sm" disabled={!draft.trim() || busy || !idle} onClick={send}>
              {busy ? <Spinner size={12} /> : "重新发送"}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group fadein flex flex-col items-end">
      {item.attachments && item.attachments.length > 0 && (
        <div className="mb-1.5 flex max-w-[80%] flex-wrap justify-end gap-1.5">
          {item.attachments.map((a) =>
            a.thumb ? (
              <img key={a.id} src={a.thumb} alt={a.name} title={a.name} className="h-20 max-w-[160px] rounded-xl border border-line object-cover" />
            ) : (
              <span key={a.id} className="rounded-lg border border-line px-2 py-1 text-[12px] text-muted">{a.name}</span>
            ),
          )}
        </div>
      )}
      <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-2xl bg-soft px-4 py-2.5 text-[14px] leading-relaxed text-fg">
        {item.kind === "review" && <SearchCheck size={14} className="mr-1.5 inline-block align-[-2px] text-muted" />}
        {item.text}
        {item.queue && !item.queued && <div className="mt-1 text-[11.5px] text-faint">{DELIVERED_LABEL[item.queue]}</div>}
      </div>
      {canBranch && idle && (
        <div className="mt-1 flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
          <Tip label="编辑后重新发送">
            <button
              onClick={() => {
                setDraft(item.text);
                setEditing(true);
              }}
              className="flex h-6 w-6 items-center justify-center rounded-md text-faint hover:bg-hover hover:text-fg"
            >
              <Pencil size={12.5} />
            </button>
          </Tip>
          <Tip label="从这里分叉成新对话">
            <button
              onClick={() => s.forkFrom(item.id)}
              className="flex h-6 w-6 items-center justify-center rounded-md text-faint hover:bg-hover hover:text-fg"
            >
              <GitFork size={12.5} />
            </button>
          </Tip>
        </div>
      )}
    </div>
  );
}

export function AssistantMessage({ item }: { item: AssistantItem }) {
  const [open, setOpen] = useState(false);
  const thinkingOnly = !item.text && !!item.reasoning;
  const { body, result: review } = useMemo(() => splitReview(item.text || ""), [item.text]);
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
      {body && (
        <div className={clsx(item.streaming && "stream-caret-wrap")}>
          <Markdown text={body} />
        </div>
      )}
      {review && !item.streaming && <ReviewCard result={review} />}
    </div>
  );
}

// ------------------------------------------------------------ 工具
function exploreLabel(it: ToolItem): { verb: string; target: string; icon: React.ReactNode; file?: string } {
  const a = it.args || {};
  if (it.name === "read") {
    const k = it.meta?.kind;
    const p = String(a.path ?? "");
    return { verb: k === "list" ? "列出" : "读取", target: shortPath(p, 3), icon: <FileSearch size={13} />, file: k === "list" ? undefined : p };
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
              {l.file && t.status !== "error" ? (
                <PathLink path={l.file} label={l.target} className="truncate text-fg/80" />
              ) : (
                <span className="mono truncate text-fg/80">{l.target}</span>
              )}
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
    const bg = !!item.meta?.background;
    const isOpen = open || (running && !!item.output);
    return (
      <Row
        icon={denied ? <Ban size={14} /> : <SquareTerminal size={14} />}
        title={
          <>
            {running ? (bg ? "正在后台启动" : "正在运行") : denied ? "未运行" : bg ? (item.meta?.running ? "后台运行" : "后台运行了") : "运行了"}{" "}
            <Mono>{cmd.split("\n")[0].slice(0, 120)}</Mono>
            {bg && item.meta?.bg_id && <span className="ml-1.5 rounded bg-soft px-1 text-[11px] text-muted">{item.meta.bg_id}</span>}
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
            {verb}{" "}
            {files.length > 1 ? (
              <span className="mono text-fg/90">{files.length} 个文件</span>
            ) : (
              <PathLink path={path} label={shortPath(path, 3)} className="text-fg/90" />
            )}
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
        {files.length > 1 && (
          <div className="mb-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[12.5px] text-muted">
            {files.map((f) => (
              <PathLink key={f.path} path={f.path} label={shortPath(f.path, 3)} />
            ))}
          </div>
        )}
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

  if (["shell_output", "shell_write", "shell_kill"].includes(item.name)) {
    const id = String(a.id ?? item.meta?.bg_id ?? "");
    const verb =
      item.name === "shell_kill" ? (running ? "正在结束" : "结束了") : item.name === "shell_write" ? (running ? "正在输入到" : "输入到") : running ? "正在查看" : "查看了";
    const input = item.name === "shell_write" ? String(a.input ?? "").replace(/\n$/, "") : "";
    const cmd = String(item.meta?.command ?? "");
    return (
      <Row
        icon={<SquareTerminal size={14} />}
        title={
          <>
            {verb}后台命令 <Mono>{id}</Mono>
            {input && (
              <>
                {" "}
                <Mono>{input.split("\n")[0].slice(0, 60)}</Mono>
              </>
            )}
            {cmd && <span className="ml-1.5 truncate text-[12px] text-faint">{cmd.slice(0, 60)}</span>}
          </>
        }
        active={running}
        tone={failed ? "error" : undefined}
        open={open}
        onToggle={() => setOpen(!open)}
      >
        <OutputBox text={item.output || ""} />
      </Row>
    );
  }
  if (item.name === "todo") return null; // 计划单独渲染
  if (item.name === "ask_user" && !failed) return null; // 提问卡片单独渲染
  if (item.name === "present_plan" && item.meta?.kind === "plan_proposal") return <PlanProposalCard item={item} />;
  if (item.name === "task" || item.meta?.kind === "subagent") return <SubagentCard item={item} />;
  if (item.name === "web_search") return <WebSearchCard item={item} />;
  if (item.name === "web_fetch") return <WebFetchCard item={item} />;
  if (item.name === "skill") return <SkillCard item={item} />;

  const mcp = item.meta?.kind === "mcp";
  return (
    <Row
      icon={mcp ? <Plug size={14} /> : <Wrench size={14} />}
      title={
        mcp ? (
          <>
            {running ? "正在调用" : "调用了"} <Mono>{String(item.meta?.tool ?? item.name)}</Mono>
            <span className="ml-1 text-faint">（MCP · {String(item.meta?.server ?? "")}）</span>
          </>
        ) : (
          <>
            {running ? "正在使用" : "使用了"} <Mono>{item.name}</Mono>
          </>
        )
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
    item.status === "once"
      ? "已允许一次"
      : item.status === "always"
        ? "已允许（本会话内不再询问）"
        : item.status === "forever"
          ? `已永久允许（本项目：${item.rule ?? ""}）`
          : item.status === "deny"
            ? "已拒绝"
            : "已取消";
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
  if (item.tool === "budget") return <BudgetCard item={item} />;
  return (
    <div className="fadein overflow-hidden rounded-xl border border-warn/40 bg-elev shadow-[var(--shadow)]">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5 text-[13px] font-medium">
        <ShieldAlert size={15} className="text-warn" />
        需要你的批准
        {item.source && (
          <span
            className="ml-auto flex min-w-0 items-center gap-1 rounded-md bg-soft px-1.5 py-0.5 text-[11.5px] font-normal text-muted"
            title={`子代理 ${item.source.agent}：${item.source.description}`}
            data-testid="approval-source"
          >
            <Bot size={12} className="shrink-0" />
            <span className="truncate">
              来自子代理 {item.source.agent} · {item.source.description}
            </span>
          </span>
        )}
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
        {item.rule && (
          <Tip label={`写入永久规则 ${item.rule}，本项目以后的对话都不再询问；可在 设置 → 工具与记忆 里删除`}>
            <Button variant="ghost" size="sm" onClick={() => respond(item.call_id, "forever")}>
              本项目以后都允许
            </Button>
          </Tip>
        )}
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

// ------------------------------------------------------------ 花费上限（D5-9）：达到上限时暂停询问
export function BudgetCard({ item }: { item: ApprovalItem }) {
  const respond = useApp((s) => s.respond);
  return (
    <div data-testid="budget-card" className="fadein overflow-hidden rounded-xl border border-warn/40 bg-elev shadow-[var(--shadow)]">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5 text-[13px] font-medium">
        <ShieldAlert size={15} className="text-warn" />
        已达到花费上限
      </div>
      <div className="px-4 py-3 text-[13px] text-fg">{item.title}</div>
      <div className="flex flex-wrap items-center gap-2 border-t border-line bg-soft/60 px-4 py-2.5">
        <Button variant="primary" size="sm" onClick={() => respond(item.call_id, "once")}>
          继续这一回合
        </Button>
        <Button variant="outline" size="sm" onClick={() => respond(item.call_id, "always")} title="直到关闭这个对话都不再询问">
          本会话不再询问
        </Button>
        <div className="flex-1" />
        <span className="text-[11.5px] text-faint">上限在 设置 → 回答与花费 里修改</span>
        <Button variant="danger" size="sm" onClick={() => respond(item.call_id, "deny")}>
          停止
        </Button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------ 提问（ask_user，D4-8）
export function QuestionCard({ item }: { item: QuestionItem }) {
  const answer = useApp((s) => s.answer);
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  if (item.status !== "pending") {
    return (
      <div className="fadein flex items-start gap-2 py-0.5 text-[12.5px] text-faint">
        <MessageCircleQuestion size={13} className="mt-[3px] shrink-0" />
        <span className="min-w-0">
          {item.question}
          {item.status === "answered" ? (
            <span className="text-muted"> → {item.answer}</span>
          ) : (
            <span>（已取消）</span>
          )}
        </span>
      </div>
    );
  }
  const send = async (a: string) => {
    if (!a.trim() || busy) return;
    setBusy(true);
    await answer(item.call_id, a.trim());
    setBusy(false);
  };
  const toggle = (o: string) => setPicked(picked.includes(o) ? picked.filter((x) => x !== o) : [...picked, o]);
  return (
    <div className="fadein overflow-hidden rounded-xl border border-accent/40 bg-elev shadow-[var(--shadow)]">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2.5 text-[13px] font-medium">
        <MessageCircleQuestion size={15} className="text-accent" />
        ee 想问你
      </div>
      <div className="px-4 py-3">
        <div className="mb-3 whitespace-pre-wrap text-[13.5px] text-fg">{item.question}</div>
        {item.options.length > 0 && (
          <div className="mb-3 flex flex-col gap-1.5">
            {item.options.map((o, i) =>
              item.multiple ? (
                <label key={i} className="flex cursor-pointer items-center gap-2 rounded-lg border border-line px-3 py-2 text-[13px] hover:bg-soft">
                  <input type="checkbox" checked={picked.includes(o)} onChange={() => toggle(o)} />
                  {o}
                </label>
              ) : (
                <button
                  key={i}
                  disabled={busy}
                  onClick={() => send(o)}
                  className="rounded-lg border border-line px-3 py-2 text-left text-[13px] text-fg transition hover:border-accent hover:bg-soft disabled:opacity-50"
                >
                  <span className="mr-2 text-faint">{i + 1}.</span>
                  {o}
                </button>
              ),
            )}
          </div>
        )}
        <div className="flex items-center gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) send(item.multiple && picked.length ? [...picked, text].filter(Boolean).join("；") : text);
            }}
            placeholder={item.options.length ? "或者直接写你的回答，回车发送" : "写下你的回答，回车发送"}
            className="h-8 min-w-0 flex-1 rounded-lg border border-line bg-bg px-2.5 text-[13px] outline-none focus:border-accent"
          />
          {item.multiple && item.options.length > 0 ? (
            <Button size="sm" variant="primary" disabled={busy || (!picked.length && !text.trim())} onClick={() => send([...picked, text].filter((x) => x.trim()).join("；"))}>
              提交{picked.length ? `（${picked.length}）` : ""}
            </Button>
          ) : (
            <Button size="sm" variant="primary" disabled={busy || !text.trim()} onClick={() => send(text)}>
              回答
            </Button>
          )}
        </div>
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
    item.reason === "interrupted"
      ? "已停止"
      : item.reason === "error"
        ? "出错"
        : item.reason === "max_steps"
          ? "达到步数上限"
          : item.reason === "loop"
            ? "检测到重复操作，已停止"
            : item.reason === "blocked"
              ? "消息被 Hook 拦截"
              : item.reason === "budget"
                ? "达到花费上限，已停止"
                : "完成";
  const bits = [`${reason} · ${formatDuration(item.duration_ms)}`];
  if (u.hit_rate !== undefined && u.hit_rate !== null) bits.push(`缓存命中 ${Math.round(u.hit_rate * 100)}%`);
  if (u.reasoning) bits.push(`推理 ${u.reasoning.toLocaleString()} tokens`);
  if (u.cost !== undefined && u.cost !== null) bits.push(`累计 ${u.currency ?? ""}${u.cost.toFixed(4)}`);
  const models = Object.entries(u.by_model ?? {});
  const tip =
    models.length > 1
      ? models.map(([m, b]) => `${m}：${b.requests} 次 · ${b.cost === null ? "价格未知" : `${b.currency}${b.cost.toFixed(4)}`}`).join("\n")
      : undefined;
  return (
    <div className="flex items-center gap-3 py-1 text-[11.5px] text-faint">
      <div className="h-px flex-1 bg-line" />
      <span title={tip} className={clsx((item.reason === "loop" || item.reason === "budget") && "text-warn")}>
        {bits.join(" · ")}
        {models.length > 1 ? ` · ${models.length} 个模型` : ""}
      </span>
      <div className="h-px flex-1 bg-line" />
    </div>
  );
}
