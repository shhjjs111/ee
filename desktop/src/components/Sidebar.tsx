import clsx from "clsx";
import {
  GitFork,
  ChevronRight,
  Folder,
  FolderPlus,
  MoreHorizontal,
  Pencil,
  Search,
  Settings as SettingsIcon,
  SquarePen,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { relTime } from "../lib/reduce";
import { useApp } from "../lib/store";
import type { ThreadSummary } from "../types";
import { Button, Menu, Spinner, Tip } from "./ui";

function norm(p: string) {
  return p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

function ThreadRow({ t, active }: { t: ThreadSummary; active: boolean }) {
  const s = useApp();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(t.title);
  return (
    <div
      className={clsx(
        "group relative flex h-8 cursor-default items-center gap-2 rounded-lg pl-7 pr-1.5 text-[13px] transition-colors",
        active ? "bg-active text-fg" : "text-muted hover:bg-hover hover:text-fg",
      )}
      onClick={() => !editing && s.openThread(t.id)}
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
              s.rpc?.call("thread/rename", { thread_id: t.id, title });
              setEditing(false);
            } else if (e.key === "Escape") setEditing(false);
          }}
          className="h-6 min-w-0 flex-1 rounded border border-accent bg-bg px-1.5 text-[13px] text-fg outline-none"
        />
      ) : (
        <>
          {t.parent_id && (
            <Tip label="分叉出来的对话">
              <GitFork size={12} className="shrink-0 text-faint" />
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
            {
              label: "删除对话",
              icon: <Trash2 size={13} />,
              danger: true,
              onSelect: () => {
                if (confirm(`删除对话“${t.title}”？此操作不能撤销。`)) s.rpc?.call("thread/delete", { thread_id: t.id });
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
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [searching, setSearching] = useState(false);
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
      } catch {
        if (live) setHits({});
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
    const map = new Map<string, { path: string; name: string; threads: ThreadSummary[] }>();
    for (const w of s.workspaces) map.set(norm(w.path), { path: w.path, name: w.name, threads: [] });
    for (const t of s.threads) {
      if (q && !t.title.toLowerCase().includes(q) && !(t.id in hits)) continue;
      const k = norm(t.cwd || "");
      if (!map.has(k)) {
        const name = (t.cwd || "其他").replace(/\\/g, "/").split("/").filter(Boolean).pop() || t.cwd;
        map.set(k, { path: t.cwd, name, threads: [] });
      }
      map.get(k)!.threads.push(t);
    }
    return [...map.values()].filter((g) => !q || g.threads.length);
  }, [s.workspaces, s.threads, q, hits]);

  const pickFolder = async () => {
    const p = window.ee ? await window.ee.openFolder() : window.prompt("项目文件夹路径");
    if (p) s.addWorkspace(p);
  };

  return (
    <aside className="flex w-[264px] shrink-0 flex-col border-r border-line bg-sidebar">
      <div className="drag flex h-11 shrink-0 items-center gap-2 px-4" style={{ paddingLeft: "var(--traffic-w)" }}>
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
                    items={[
                      { label: "在资源管理器中打开", onSelect: () => window.ee?.openPath(g.path) },
                      { label: "从列表移除", danger: true, onSelect: () => s.removeWorkspace(g.path) },
                    ]}
                  />
                </span>
              </div>
              {!isCollapsed && (
                <div className="space-y-px">
                  {g.threads.slice(0, 50).map((t) => (
                    <div key={t.id}>
                      <ThreadRow t={t} active={t.id === s.activeThreadId && s.view === "chat"} />
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
    </aside>
  );
}
