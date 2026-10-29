import { openPath } from "../lib/openPath";
import clsx from "clsx";
import { BookText, ChevronDown, FolderOpen, FolderPlus, GitBranch, PanelRight, PanelRightClose } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { formatDuration, groupBlocks, shortPath } from "../lib/reduce";
import { splitQueued } from "../lib/queue";
import { useApp } from "../lib/store";
import type { Item } from "../types";
import { SubagentGroup } from "./AgentItems";
import { Composer } from "./Composer";
import {
  ApprovalCard,
  AssistantMessage,
  ErrorRow,
  ExploreGroup,
  NoticeRow,
  PlanCard,
  QuestionCard,
  ToolCard,
  TurnEnd,
  UserMessage,
} from "./Items";
import { Button, Select, Tip } from "./ui";

function renderItem(it: Item, approvals: Map<string, string>) {
  switch (it.type) {
    case "user":
      return <UserMessage item={it} />;
    case "assistant":
      return <AssistantMessage item={it} />;
    case "tool":
      return <ToolCard item={it} approval={approvals.get(it.call_id)} />;
    case "approval":
      // 批准过的不单独占一行（工具行上有小盾牌）；待批准和拒绝的显示
      if (it.status === "once" || it.status === "always" || it.status === "forever") return null;
      return <ApprovalCard item={it} />;
    case "question":
      return <QuestionCard item={it} />;
    case "plan":
      return <PlanCard item={it} />;
    case "notice":
      return <NoticeRow text={it.text} level={it.level} />;
    case "error":
      return <ErrorRow message={it.message} />;
    case "turn_end":
      return <TurnEnd item={it} />;
  }
}

function Working({ since, waiting, text }: { since: number; waiting: boolean; text?: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="flex items-center gap-2 py-1 text-[13px]">
      <span className={clsx("min-w-0 truncate", waiting ? "text-warn" : "shimmer")} title={!waiting ? text : undefined}>
        {waiting ? "等待你的批准" : text || "正在工作"}
      </span>
      <span className="text-faint">{formatDuration(now - since)}</span>
      {!waiting && <span className="text-[11.5px] text-faint">· Esc 停止</span>}
    </div>
  );
}

function Header() {
  const s = useApp();
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const cwd = d?.cwd ?? s.activeWorkspace;
  return (
    <div
      className="drag flex h-11 shrink-0 items-center gap-2 border-b border-line pl-5"
      style={{ paddingRight: s.rightOpen ? 12 : "calc(var(--overlay-w) + 12px)" }}
    >
      <div className="min-w-0 truncate text-[13.5px] font-medium text-fg">{d ? d.title : "新对话"}</div>
      {cwd && (
        <Tip label={cwd} side="bottom">
          <button
            className="no-drag flex min-w-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[12px] text-faint hover:bg-hover hover:text-muted"
            onClick={() => openPath(cwd)}
          >
            <FolderOpen size={12} />
            <span className="truncate">{shortPath(cwd, 1)}</span>
          </button>
        </Tip>
      )}
      {d?.git?.is_repo && (
        <span className="flex items-center gap-1 text-[12px] text-faint">
          <GitBranch size={12} />
          {d.git.branch}
        </span>
      )}
      {d?.instructions && d.instructions.length > 0 && (
        <Tip
          side="bottom"
          label={
            <div className="max-w-[420px]">
              <div className="mb-1 font-medium">已加载项目说明</div>
              {d.instructions.map((p) => (
                <div key={p} className="mono break-all text-[11.5px] opacity-80">
                  {p}
                </div>
              ))}
            </div>
          }
        >
          <span className="no-drag flex cursor-default items-center gap-1 rounded-md px-1.5 py-0.5 text-[12px] text-faint hover:bg-hover hover:text-muted">
            <BookText size={12} />
            AGENTS.md{d.instructions.length > 1 ? ` ×${d.instructions.length}` : ""}
          </span>
        </Tip>
      )}
      <div className="flex-1" />
      <Tip label={s.rightOpen ? "收起右侧面板" : "展开右侧面板"} side="bottom">
        <Button
          size="icon"
          onClick={() => {
            localStorage.setItem("ee.right", s.rightOpen ? "0" : "1");
            s.set({ rightOpen: !s.rightOpen });
          }}
        >
          {s.rightOpen ? <PanelRightClose size={16} /> : <PanelRight size={16} />}
        </Button>
      </Tip>
    </div>
  );
}

const SUGGESTIONS = [
  "解释一下这个项目的结构和入口",
  "找出并修复测试里失败的用例",
  "给最近改动写单元测试",
  "审查当前未提交的改动",
];

function EmptyState() {
  const s = useApp();
  const pickFolder = async () => {
    const p = window.ee ? await window.ee.openFolder() : window.prompt("项目文件夹路径");
    if (p) void s.addWorkspace(p);
  };
  const options = s.workspaces.map((w) => ({ value: w.path, label: w.name, hint: w.path }));
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-8 pb-10">
      <div className="mb-2 flex h-11 w-11 items-center justify-center rounded-2xl bg-fg text-[17px] font-semibold tracking-tight text-bg">
        ee
      </div>
      <h1 className="mb-1 text-[22px] font-semibold tracking-tight text-fg">我们来做点什么？</h1>
      <div className="mb-8 flex items-center gap-1 text-[13px] text-muted">
        {s.activeWorkspace ? (
          <>
            在
            <Select
              value={s.activeWorkspace}
              options={[...options, { value: "__add__", label: "添加文件夹…", icon: <FolderPlus size={14} /> }]}
              onChange={(v) => (v === "__add__" ? pickFolder() : s.newThread(v))}
              icon={<FolderOpen size={13} />}
              side="bottom"
              className="text-fg"
              renderLabel={(o) => o?.label ?? shortPath(s.activeWorkspace!, 1)}
            />
            里工作
          </>
        ) : (
          <Button variant="outline" onClick={pickFolder}>
            <FolderPlus size={14} /> 选择项目文件夹
          </Button>
        )}
      </div>
      <div className="w-full max-w-[720px]">
        <Composer autoFocus />
        <div className="mt-4 grid grid-cols-2 gap-2">
          {SUGGESTIONS.map((t) => (
            <button
              key={t}
              disabled={!s.activeWorkspace}
              onClick={() => s.send(t)}
              className="rounded-xl border border-line px-3.5 py-2.5 text-left text-[13px] text-muted transition-colors hover:bg-hover hover:text-fg disabled:opacity-40"
            >
              {t}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export function Conversation() {
  const s = useApp();
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const scroller = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);
  const blocks = useMemo(() => groupBlocks(splitQueued(d?.items ?? []).visible), [d?.items]); // 排队中的显示在输入框上方
  const approvals = useMemo(() => {
    const m = new Map<string, string>();
    for (const it of d?.items ?? []) if (it.type === "approval") m.set(it.call_id, it.status);
    return m;
  }, [d?.items]);
  const running = !!d && d.status !== "idle";
  const since = d ? s.turnStartedAt[d.id] : undefined;

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick) el.scrollTop = el.scrollHeight;
  }, [d?.items, stick, running]);

  useEffect(() => setStick(true), [s.activeThreadId]);

  if (!d) {
    return (
      <div className="flex min-w-0 flex-1 flex-col bg-bg">
        <Header />
        <EmptyState />
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-1 flex-col bg-bg">
      <Header />
      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
        }}
        className="relative flex-1 overflow-y-auto"
      >
        {/* 间距分两档：用户发问是"回合起点"，前面留更大空白；其余条目紧凑跟随。
            改版前统一 space-y-3，一问一答挤成一块，读不出回合边界。 */}
        <div className="mx-auto max-w-[780px] px-6 pb-6 pt-6">
          {blocks.map((b) =>
            b.kind === "explore" ? (
              <div key={b.id} className="mt-3 first:mt-0">
                <ExploreGroup items={b.items} />
              </div>
            ) : b.kind === "agents" ? (
              <div key={b.id} className="mt-3 first:mt-0">
                <SubagentGroup items={b.items} />
              </div>
            ) : (
              <div
                key={b.item.id}
                className={b.item.type === "user" ? "mt-7 first:mt-0" : "mt-3 first:mt-0"}
              >
                {renderItem(b.item, approvals)}
              </div>
            ),
          )}
          {running && (
            <div className="mt-3">
              <Working since={since ?? Date.now()} waiting={d.status === "waiting_approval"} text={d.status_text} />
            </div>
          )}
        </div>
      </div>
      {!stick && (
        <div className="pointer-events-none relative">
          <button
            onClick={() => setStick(true)}
            className="pointer-events-auto absolute -top-12 left-1/2 flex h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full border border-line bg-elev text-muted shadow-[var(--shadow)] hover:text-fg"
          >
            <ChevronDown size={16} />
          </button>
        </div>
      )}
      <div className="mx-auto w-full max-w-[780px] px-6 pb-5">
        <Composer autoFocus />
      </div>
    </div>
  );
}
