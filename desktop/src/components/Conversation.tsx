import { openPath } from "../lib/openPath";
import clsx from "clsx";
import { BookText, ChevronDown, FolderOpen, FolderPlus, GitBranch, PanelRight, PanelRightClose } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { formatDuration, groupBlocks, shortPath, type Block } from "../lib/reduce";
import { splitQueued } from "../lib/queue";
import { useApp } from "../lib/store";
import { selectActiveDetail, selectActiveThreadId, selectActiveTurnStartedAt } from "../lib/selectors";
import { useAppFields } from "../lib/useAppFields";
import { TITLEBAR_H } from "./shell/Titlebar";
import type { Item, ToolItem } from "../types";
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

function renderItem(it: Item, approval?: string) {
  switch (it.type) {
    case "user":
      return <UserMessage item={it} />;
    case "assistant":
      return <AssistantMessage item={it} />;
    case "tool":
      return <ToolCard item={it} approval={approval} />;
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

/** 窗口化的渲染单元：条目对象引用不变就不重渲（流式 delta 只换一个条目）。 */
const ItemBlock = memo(function ItemBlock({ item, approval }: { item: Item; approval?: string }) {
  return (
    <div data-block={item.id} className={item.type === "user" ? "mt-7 first:mt-0" : "mt-3 first:mt-0"}>
      {renderItem(item, approval)}
    </div>
  );
});

const sameItems = (a: { items: Item[] }, b: { items: Item[] }) =>
  a.items.length === b.items.length && a.items.every((x, i) => x === b.items[i]);

const ExploreBlock = memo(function ExploreBlock({ id, items }: { id: string; items: Item[] }) {
  return (
    <div data-block={id} className="mt-3 first:mt-0">
      <ExploreGroup items={items} />
    </div>
  );
}, sameItems);

const AgentsBlock = memo(function AgentsBlock({ id, items }: { id: string; items: Item[] }) {
  return (
    <div data-block={id} className="mt-3 first:mt-0">
      <SubagentGroup items={items as ToolItem[]} />
    </div>
  );
}, sameItems);

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
  // 窄订阅：标题栏只关心当前会话详情 / 项目路径 / 右侧面板开关。
  const d = useApp(selectActiveDetail);
  const activeWorkspace = useApp((s) => s.activeWorkspace);
  const rightOpen = useApp((s) => s.rightOpen);
  const set = useApp((s) => s.set);
  const cwd = d?.cwd ?? activeWorkspace;
  return (
    <div
      className="drag flex shrink-0 items-center gap-2 border-b border-line pl-5"
      style={{ height: TITLEBAR_H, paddingRight: rightOpen ? 12 : "calc(var(--overlay-w) + 12px)" }}
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
      <Tip label={rightOpen ? "收起右侧面板" : "展开右侧面板"} side="bottom">
        <Button
          size="icon"
          onClick={() => {
            localStorage.setItem("ee.right", rightOpen ? "0" : "1");
            set({ rightOpen: !rightOpen });
          }}
        >
          {rightOpen ? <PanelRightClose size={16} /> : <PanelRight size={16} />}
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
  // 窄订阅：空状态只在首次打开时显示，仍需避免另一会话每帧 delta 带着它重渲。
  const s = useAppFields("workspaces", "activeWorkspace", "addWorkspace", "newThread", "send");
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

const PAGE = 60;

function blockKey(b: Block): string {
  return b.kind === "item" ? b.item.id : b.id;
}

export function Conversation() {
  // 窄订阅（T22）：只订阅活动会话 id、其详情对象和计时起点。其他会话的 delta flush
  // 会重建 details Record，但本会话的 ThreadDetail 引用不变 → 本组件不重渲染。
  const activeThreadId = useApp(selectActiveThreadId);
  const d = useApp(selectActiveDetail);
  const since = useApp(selectActiveTurnStartedAt);
  const scroller = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);
  const blocks = useMemo(() => groupBlocks(splitQueued(d?.items ?? []).visible), [d?.items]); // 排队中的显示在输入框上方
  const approvals = useMemo(() => {
    const m = new Map<string, string>();
    for (const it of d?.items ?? []) if (it.type === "approval") m.set(it.call_id, it.status);
    return m;
  }, [d?.items]);
  const running = !!d && d.status !== "idle";

  // ---------------------------------------------------------------- 区块窗口化
  // 大会话一次把上千个区块挂进 DOM：每个块都要跑一次 ReactMarkdown 解析、每个代码块
  // 还要跑一次 Shiki 高亮，实测 1120 块时打开一次卡 4 秒（单个长任务 3.5 秒）。
  // 而屏幕一屏只放得下几块 —— 其余全是白做工。这里只渲染「从锚点开始到末尾」的窗口，
  // 往上翻时按批追加。锚点用块 id 记而不是下标：新的 delta 只会往尾部增长，
  // 记住 id 才能保证用户正在看的历史不会被滑动窗口挤掉。
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const anchorIdx = anchorId ? blocks.findIndex((b) => blockKey(b) === anchorId) : -1;
  // 锚点失效（切会话 / 撤回消息）时回到「尾部一屏」
  const start = anchorIdx >= 0 ? anchorIdx : Math.max(0, blocks.length - PAGE);
  const visible = start > 0 ? blocks.slice(start) : blocks;
  const hidden = start;

  // 展开更早的一批。滚动位置由浏览器原生的 overflow-anchor 保持（往上方插入内容时
  // 不动视口）—— 实测它比自己算 scrollHeight 差量可靠得多：新一批里的 Markdown /
  // 代码高亮是异步完成的，自己量高度那一刻还没长完，补得不够就会往上弹。
  const loadEarlierFromStore = useApp((s) => s.loadEarlier);
  // 本地窗口后面还有（hidden > 0）就纯前端展开；本地已到底但后端还有（items_has_more）
  // 才去拉历史。首屏只拉了尾部一页（store.FIRST_PAGE），几千条的长会话不会一次全下。
  const serverMore = !!d?.items_has_more;
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const loadEarlier = useCallback(async () => {
    if (start > 0) {
      const next = Math.max(0, start - PAGE);
      setAnchorId(blockKey(blocks[next]));
      return;
    }
    if (!serverMore || loadingEarlier || !activeThreadId) return;
    setLoadingEarlier(true);
    try {
      await loadEarlierFromStore(activeThreadId);
    } finally {
      setLoadingEarlier(false);
    }
  }, [blocks, start, serverMore, loadingEarlier, activeThreadId, loadEarlierFromStore]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (stick) el.scrollTop = el.scrollHeight;
  }, [d?.items, stick, running, start]);

  useEffect(() => setStick(true), [activeThreadId]);
  // 切会话时丢掉锚点，回到新会话的尾部
  useEffect(() => {
    setAnchorId(null);
  }, [activeThreadId]);

  // 真·新对话（没有 activeThreadId）才是空状态。有 id 但详情还没到时（openThread 先 set
  // activeThreadId，thread/get 还在路上）是「正在打开」——原来两种情况共用同一个 `!d`
  // 分支，于是加载已有会话时会闪出「我们来做点什么」：用户以为切成了新对话，在这里打的字
  // 会在详情到达、切回正常分支时随组件卸载直接丢掉。
  if (!activeThreadId) {
    return (
      <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-bg">
        <Header />
        <EmptyState />
      </div>
    );
  }

  if (!d) {
    return (
      <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-bg">
        <Header />
        <div className="flex flex-1 items-center justify-center text-[13px] text-faint">正在打开对话…</div>
      </div>
    );
  }

  return (
    // min-h-0 是防御性约束（与 AppShell 同款）：flex 子项默认 min-height:auto，
    // 理论上长内容可以把这层撑爆、把滚动区和侧栏顶出视口。2026-10-30 真机出现过
    // 一次该形态的坏布局（容器被撑到 5046px），但复现验证未能确证本层缺 min-h-0
    // 是根因（回滚后用真实数据遍历全部会话均正常）。保留约束作为纵深防御，
    // 真正根因待坏状态再次出现时用现场 DevTools 数据定位。
    <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-bg">
      <Header />
      <div
        ref={scroller}
        data-testid="conversation-scroller"
        onScroll={(e) => {
          const el = e.currentTarget;
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          setStick(atBottom);
          // 离开底部时把当前窗口起点钉住（否则新 delta 会让窗口滑动，正在看的内容被挤走）。
          // 滚到接近顶部再自动补一批，往上翻不用一次次点按钮。
          if (!atBottom && !anchorId && hidden > 0) setAnchorId(blockKey(blocks[start]));
          if (!atBottom && el.scrollTop < 240 && (hidden > 0 || serverMore)) void loadEarlier();
        }}
        className="relative flex-1 overflow-y-auto"
      >
        {/* 间距分两档：用户发问是"回合起点"，前面留更大空白；其余条目紧凑跟随。
            改版前统一 space-y-3，一问一答挤成一块，读不出回合边界。 */}
        <div className="mx-auto max-w-[780px] px-6 pb-6 pt-6">
          {(hidden > 0 || serverMore) && (
            <div className="mb-3 flex justify-center">
              <button
                onClick={() => {
                  // 先回到顶部再从同一入口加载：两条路径（点按钮 / 滚到顶）行为一致，
                  // 也都靠原生 scroll anchoring 保住视口位置。
                  if (scroller.current) scroller.current.scrollTop = 0;
                  void loadEarlier();
                }}
                disabled={loadingEarlier}
                data-testid="load-earlier"
                className="rounded-full border border-line bg-elev px-3 py-1 text-[12px] text-muted transition-colors hover:bg-hover hover:text-fg disabled:opacity-60"
              >
                {loadingEarlier
                  ? "正在加载更早的对话…"
                  : hidden > 0
                    ? `显示更早的 ${Math.min(PAGE, hidden)} 条（还有 ${hidden} 条）`
                    : "加载更早的历史"}
              </button>
            </div>
          )}
          {visible.map((b) =>
            b.kind === "explore" ? (
              <ExploreBlock key={b.id} id={b.id} items={b.items} />
            ) : b.kind === "agents" ? (
              <AgentsBlock key={b.id} id={b.id} items={b.items} />
            ) : (
              <ItemBlock key={b.item.id} item={b.item} approval={approvals.get((b.item as { call_id?: string }).call_id ?? "")} />
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
