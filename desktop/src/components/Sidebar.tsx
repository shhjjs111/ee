import { reportError } from "../lib/errors";
import { openPath } from "../lib/openPath";
import clsx from "clsx";
import {
  GitBranch,
  GitFork,
  CornerDownRight,
  ChevronRight,
  Clock,
  Download,
  Folder,
  GitCompare,
  FolderPlus,
  MoreHorizontal,
  PackageOpen,
  Pencil,
  Search,
  Settings as SettingsIcon,
  SquarePen,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { relTime } from "../lib/reduce";
import { ChildThreadCard } from "./ChildThreadCard";
import { useApp } from "../lib/store";
import { useAutomations } from "../lib/automationStore";
import type { ThreadSummary } from "../types";
import { Button, Menu, Spinner, Tip } from "./ui";
import { TITLEBAR_H } from "./shell/Titlebar";

function norm(p: string) {
  return p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

function ThreadRow({ t, active }: { t: ThreadSummary; active: boolean }) {
  const s = useApp();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(t.title);
  // 指挥会话派发的子任务缩进一级（D6-1 U8）。只认 orchestrator_parent_id：
  // parent_id 是普通分叉的来源（branch.py 写入），分叉出来的对话仍是平级会话，缩进会误导。
  const isChild = !!t.orchestrator_parent_id;
  return (
    <div
      className={clsx(
        "group relative flex h-8 cursor-default items-center gap-2 rounded-lg pr-1.5 text-[13px] transition-colors",
        isChild ? "pl-11" : "pl-7",
        active ? "bg-active text-fg" : "text-muted hover:bg-hover hover:text-fg",
      )}
      onClick={() => !editing && s.openThread(t.id)}
      data-testid="thread-row"
      data-title={t.title}
    >
      {editing ? (
        <input
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              void s.renameThread(t.id, title);
              setEditing(false);
            } else if (e.key === "Escape") setEditing(false);
          }}
          className="h-6 min-w-0 flex-1 rounded border border-accent bg-bg px-1.5 text-[13px] text-fg outline-none"
        />
      ) : (
        <>
          {t.worktree && (
            <Tip label={`在 worktree 里：${t.worktree.branch}`}>
              <GitBranch size={12} className="shrink-0 text-accent" />
            </Tip>
          )}
          {t.parent_id && (
            <Tip label="分叉出来的对话">
              <GitFork size={12} className="shrink-0 text-faint" />
            </Tip>
          )}
          {isChild && (
            <Tip label="父会话派发的子任务">
              <CornerDownRight size={12} className="shrink-0 text-accent" />
            </Tip>
          )}
          <span className="min-w-0 flex-1 truncate">{t.title}</span>
        </>
      )}
      {t.status === "running" && <Spinner size={12} className="shrink-0 text-muted" />}
      {t.status === "waiting_approval" && (
        <Tip label="等待批准">
          <span className="h-2 w-2 shrink-0 rounded-full bg-warn" />
        </Tip>
      )}
      {t.status === "idle" && !editing && (
        <span className="shrink-0 text-[11px] text-faint group-hover:hidden">{relTime(t.updated)}</span>
      )}
      <div className="hidden shrink-0 group-hover:block" onClick={(e) => e.stopPropagation()}>
        <Menu
          trigger={
            <button className="flex h-6 w-6 items-center justify-center rounded-md text-muted hover:bg-active hover:text-fg">
              <MoreHorizontal size={14} />
            </button>
          }
          items={[
            { label: "重命名", icon: <Pencil size={13} />, onSelect: () => { setTitle(t.title); setEditing(true); } },
            { label: "导出…", icon: <Download size={13} />, onSelect: () => s.set({ exportDialog: t.id }) },
            {
              label: "删除对话",
              icon: <Trash2 size={13} />,
              danger: true,
              onSelect: () => {
                if (confirm(`删除对话“${t.title}”？此操作不能撤销。`)) void s.deleteThread(t.id);
              },
            },
          ]}
        />
      </div>
    </div>
  );
}

export function Sidebar() {
  const s = useApp();
  const autoUnread = useAutomations((x) => x.unread);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // 每个项目默认显示 50 条，点"再显示"每次多 50（改版前是硬截断到 50，多出的静默消失）。
  const [showMore, setShowMore] = useState<Record<string, number>>({});
  const PAGE = 50;
  const shown = (key: string) => PAGE + (showMore[key] ?? 0);
  const [searching, setSearching] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  // Ctrl/⌘+K：打开并聚焦对话搜索（侧栏按钮上提示的快捷键）；终端里不抢
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "k") return;
      if ((e.target as HTMLElement | null)?.closest?.(".xterm")) return;
      e.preventDefault();
      setSearching(true);
      requestAnimationFrame(() => {
        searchRef.current?.focus();
        searchRef.current?.select();
      });
      searchRef.current?.focus();
      searchRef.current?.select();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const q = s.search.trim().toLowerCase();
  // 全文搜索（D4-4）：输入停顿 250ms 后查后端索引；标题命中或内容命中都显示，内容命中附片段
  const [hits, setHits] = useState<Record<string, string>>({});
  const [searchBusy, setSearchBusy] = useState(false);
  useEffect(() => {
    if (!q || !s.rpc) {
      setHits({});
      return;
    }
    let live = true;
    const timer = setTimeout(async () => {
      setSearchBusy(true);
      try {
        const r = await s.rpc!.call("thread/search", { query: q, limit: 100 });
        if (!live) return;
        const m: Record<string, string> = {};
        for (const x of r.results ?? []) m[x.thread.id] = x.hits?.find((h: any) => h.kind !== "title")?.snippet ?? "";
        setHits(m);
      } catch (e) {
        if (live) {
          setHits({});
          reportError("全文搜索失败，只按标题筛选")(e);
        }
      } finally {
        if (live) setSearchBusy(false);
      }
    }, 250);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [q, s.rpc]);

  const groups = useMemo(() => {
    // 项目组只来自 workspace/list（后端已把历史会话的目录合并进来），
    // 不再在这里"从会话 cwd 现造项目"。
    //
    // 之前两边各造一份：后端读 desktop.json，侧栏从 thread.cwd 推导。
    // 结果界面上看得见的项目不在 workspace/list 里 —— 点「从列表移除」删的是
    // 后端列表（本来就没有），点「在资源管理器中打开」白名单里也没有它，
    // 两个操作都像"点了没反应"。
    const map = new Map<string, { path: string; name: string; threads: ThreadSummary[] }>();
    for (const w of s.workspaces) map.set(norm(w.path), { path: w.path, name: w.name, threads: [] });

    // 会话找不到对应项目时（目录已删、或后端还没合并到）归到「其他」，
    // 不能静默丢弃 —— 丢的是用户的会话入口。
    const orphan: ThreadSummary[] = [];
    for (const t of s.threads) {
      if (q && !t.title.toLowerCase().includes(q) && !(t.id in hits)) continue;
      // 自动化产生的对话在「自动化」收件箱里看，不挤占项目列表（正在看的除外，D4-10）
      if (!q && (t as { source?: string }).source === "automation" && t.id !== s.activeThreadId) continue;
      const home = t.workspace || t.cwd || ""; // worktree 里的对话归到主仓库（D5-1）
      const k = norm(home);
      const g = map.get(k);
      if (g) g.threads.push(t);
      else orphan.push(t);
    }
    if (orphan.length) {
      map.set("\u0000orphan", { path: "", name: "其他", threads: orphan });
    }
    return [...map.values()].filter((g) => !q || g.threads.length);
  }, [s.workspaces, s.threads, q, hits, s.activeThreadId]);

  const pickFolder = async () => {
    const p = window.ee ? await window.ee.openFolder() : window.prompt("项目文件夹路径");
    if (p) void s.addWorkspace(p);
  };

  return (
    <aside className="flex h-full w-full shrink-0 flex-col bg-sidebar">
      <div className="drag flex shrink-0 items-center gap-2 px-4" style={{ height: TITLEBAR_H, paddingLeft: "var(--traffic-w)" }}>
        <div className="flex h-5 w-5 items-center justify-center rounded-md bg-fg text-[10px] font-bold text-bg">ee</div>
        <span className="text-[13px] font-semibold tracking-tight text-fg">ee</span>
      </div>
      <div className="space-y-0.5 px-2 pb-2">
        <button
          onClick={() => s.newThread()}
          className={clsx(
            "no-drag flex h-8 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-colors",
            !s.activeThreadId && s.view === "chat" ? "bg-active text-fg" : "text-fg hover:bg-hover",
          )}
        >
          <SquarePen size={15} /> 新对话
          <span className="ml-auto text-[11px] text-faint">Ctrl N</span>
        </button>
        {searching ? (
          <div className="flex h-8 items-center gap-2 rounded-lg border border-line bg-bg px-2.5">
            <Search size={14} className="text-faint" />
            <input
              ref={searchRef}
              autoFocus
              value={s.search}
              onChange={(e) => s.set({ search: e.target.value })}
              onKeyDown={(e) => e.key === "Escape" && (s.set({ search: "" }), setSearching(false))}
              placeholder="搜索对话标题和内容"
              className="min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-faint"
            />
            {searchBusy && <Spinner />}
            <button onClick={() => (s.set({ search: "" }), setSearching(false))} className="text-faint hover:text-fg">
              <X size={13} />
            </button>
          </div>
        ) : (
          <button
            onClick={() => setSearching(true)}
            className="no-drag flex h-8 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13px] text-fg transition-colors hover:bg-hover"
          >
            <Search size={15} /> 搜索
            <span className="ml-auto text-[11px] text-faint">Ctrl K</span>
          </button>
        )}
        <button
          onClick={() => s.set({ view: s.view === "automations" ? "chat" : "automations" })}
          className={clsx(
            "no-drag flex h-8 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-colors",
            s.view === "automations" ? "bg-active text-fg" : "text-fg hover:bg-hover",
          )}
        >
          <Clock size={15} /> 自动化
          {autoUnread > 0 && (
            <span className="ml-auto min-w-[18px] rounded-full bg-accent px-1.5 text-center text-[11px] font-medium leading-[18px] text-white">
              {autoUnread > 99 ? "99+" : autoUnread}
            </span>
          )}
        </button>
      </div>

      <div className="flex items-center justify-between px-4 pb-1 pt-2">
        <span className="text-[11.5px] font-medium text-faint">项目</span>
        <Tip label="添加项目文件夹">
          <Button size="icon-sm" onClick={pickFolder}>
            <FolderPlus size={14} />
          </Button>
        </Tip>
      </div>
      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {groups.length === 0 && (
          <div className="px-3 py-6 text-center text-[12.5px] leading-relaxed text-faint">
            {q ? "没有匹配的对话" : "还没有项目。添加一个文件夹，ee 会在里面读写代码、运行命令。"}
            {!q && (
              <div className="mt-3">
                <Button variant="outline" size="sm" onClick={pickFolder}>
                  <FolderPlus size={13} /> 添加文件夹
                </Button>
              </div>
            )}
          </div>
        )}
        {groups.map((g) => {
          const k = norm(g.path);
          const isCollapsed = collapsed[k];
          const activeWs = s.activeWorkspace && norm(s.activeWorkspace) === k;
          return (
            <div key={k} className="mb-1">
              <div
                className={clsx(
                  "group flex h-8 cursor-default items-center gap-1.5 rounded-lg px-1.5 text-[13px] transition-colors hover:bg-hover",
                  activeWs ? "text-fg" : "text-muted",
                )}
                onClick={() => setCollapsed({ ...collapsed, [k]: !isCollapsed })}
                title={g.path}
              >
                <ChevronRight size={13} className={clsx("shrink-0 text-faint transition-transform", !isCollapsed && "rotate-90")} />
                <Folder size={14} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate font-medium">{g.name}</span>
                <span className="hidden items-center group-hover:flex" onClick={(e) => e.stopPropagation()}>
                  <Tip label="在此项目中新建对话">
                    <button
                      className="flex h-6 w-6 items-center justify-center rounded-md text-muted hover:bg-active hover:text-fg"
                      onClick={() => s.newThread(g.path)}
                    >
                      <SquarePen size={13} />
                    </button>
                  </Tip>
                  <Menu
                    trigger={
                      <button className="flex h-6 w-6 items-center justify-center rounded-md text-muted hover:bg-active hover:text-fg">
                        <MoreHorizontal size={14} />
                      </button>
                    }
                    items={
                      // 「其他」分组没有对应目录（path 为空），这些操作对它无意义
                      g.path
                        ? [
                            { label: "在资源管理器中打开", onSelect: () => openPath(g.path) },
                            { label: "Worktree…", onSelect: () => s.set({ worktreeDialog: g.path }) },
                            { label: "交接清单…", icon: <PackageOpen size={13} />, onSelect: () => s.set({ handoffDialog: g.path }) },
                            { label: "比较候选…", icon: <GitCompare size={13} />, onSelect: () => s.set({ bestofDialog: g.path }) },
                            { label: "从列表移除", danger: true, onSelect: () => void s.removeWorkspace(g.path) },
                          ]
                        : []
                    }
                  />
                </span>
              </div>
              {!isCollapsed && (
                <div className="space-y-px">
                  {g.threads.slice(0, shown(k)).map((t) => (
                    <div key={t.id}>
                      <ThreadRow t={t} active={t.id === s.activeThreadId && s.view === "chat"} />
                      {/* 子会话额外给一张卡片（D6-1 U8）：状态点看不清「跑完了」还是「超预算/中断」，
                          卡片把终态、分支和改动数摊开；普通会话不显示，避免噪声。 */}
                      {!q && t.orchestrator_parent_id && (
                        <ChildThreadCard t={t} onOpen={(id) => void s.openThread(id)} />
                      )}
                      {q && hits[t.id] && (
                        <div
                          className="cursor-default truncate pb-1 pl-7 pr-2 text-[11.5px] text-faint hover:text-muted"
                          title={hits[t.id]}
                          onClick={() => s.openThread(t.id)}
                        >
                          {hits[t.id]}
                        </div>
                      )}
                    </div>
                  ))}
                  {g.threads.length === 0 && <div className="py-1 pl-7 text-[12px] text-faint">暂无对话</div>}
                  {/* 改版前固定 slice(0, 50)：第 51 个对话直接消失，界面上没有任何交代。
                      现在先给 50 条 + 明确的"显示更多"，每次多放 50；搜索时不限。 */}
                  {!q && g.threads.length > shown(k) && (
                    <button
                      onClick={() => setShowMore((m) => ({ ...m, [k]: shown(k) + PAGE }))}
                      className="no-drag mt-0.5 flex h-7 w-full items-center gap-1.5 rounded-lg pl-7 pr-2 text-left text-[12px] text-faint transition-colors hover:bg-hover hover:text-muted"
                      data-testid="threads-more"
                    >
                      再显示 {Math.min(PAGE, g.threads.length - shown(k))} 个
                      <span className="text-faint/70">（还有 {g.threads.length - shown(k)} 个）</span>
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="border-t border-line p-2">
        <button
          onClick={() => s.set({ view: s.view === "settings" ? "chat" : "settings" })}
          className={clsx(
            "flex h-8 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-colors",
            s.view === "settings" ? "bg-active text-fg" : "text-muted hover:bg-hover hover:text-fg",
          )}
        >
          <SettingsIcon size={15} /> 设置
          {s.conn !== "open" && (
            <span className="ml-auto flex items-center gap-1.5 text-[11.5px] text-warn">
              <span className="h-1.5 w-1.5 rounded-full bg-warn" /> {s.conn === "connecting" ? "连接中" : "已断开"}
            </span>
          )}
        </button>
      </div>
      {/* 四个对话框已迁到应用根部的 GlobalDialogs：侧栏折叠/换形态时入口不能再跟着消失 */}
    </aside>
  );
}
