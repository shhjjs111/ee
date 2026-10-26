import "@xterm/xterm/css/xterm.css";
import clsx from "clsx";
import { Eraser, Plus, SquareTerminal, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef } from "react";
import { useApp } from "../lib/store";
import { attach, checkAvailable, clear, closeTab, fit, newTab, useTerms } from "../lib/terminals";
import { Button, Spinner, Tip } from "./ui";

export function TerminalPanel() {
  const s = useApp();
  const t = useTerms();
  const box = useRef<HTMLDivElement>(null);
  const cwd = (s.activeThreadId ? s.details[s.activeThreadId]?.cwd : null) ?? s.activeWorkspace;

  useEffect(() => {
    checkAvailable().then((r) => {
      if (r.ok && useTerms.getState().tabs.length === 0 && cwd) newTab(cwd);
    });
  }, [cwd]);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el || t.active === null) return;
    // 只显示当前标签的 holder（其它的留在 DOM 里，保持滚动和内容）
    for (const c of Array.from(el.children) as HTMLElement[]) c.style.display = c.dataset.key === String(t.active) ? "" : "none";
    attach(t.active, el);
  }, [t.active, t.available]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    let raf = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const a = useTerms.getState().active;
        if (a !== null) fit(a);
      });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [t.available]);

  if (t.available && !t.available.ok) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
        <SquareTerminal size={28} className="mb-3 text-faint" />
        <div className="mb-1 text-[13px] font-medium text-fg">终端不可用</div>
        <div className="mono max-w-[320px] break-words text-[11.5px] leading-relaxed text-faint">{t.available.error}</div>
      </div>
    );
  }
  if (!cwd) return <div className="p-6 text-center text-[13px] text-faint">先选择一个项目</div>;

  const activeTab = t.tabs.find((x) => x.key === t.active);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-9 shrink-0 items-center gap-0.5 border-b border-line px-2">
        <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
          {t.tabs.map((tab, i) => (
            <div
              key={tab.key}
              onClick={() => useTerms.setState({ active: tab.key })}
              title={tab.cwd}
              className={clsx(
                "group flex h-6 shrink-0 cursor-default items-center gap-1.5 rounded-md pl-2 pr-1 text-[12px] transition-colors",
                tab.key === t.active ? "bg-active text-fg" : "text-muted hover:bg-hover hover:text-fg",
              )}
            >
              {tab.status === "starting" ? (
                <Spinner size={10} />
              ) : (
                <span
                  className={clsx(
                    "h-1.5 w-1.5 rounded-full",
                    tab.status === "running" ? "bg-ok" : tab.status === "error" ? "bg-danger" : "bg-faint",
                  )}
                />
              )}
              <span className="mono">{tab.shell || "shell"}</span>
              {t.tabs.length > 1 && <span className="text-faint">{i + 1}</span>}
              <button
                className="ml-0.5 flex h-4 w-4 items-center justify-center rounded opacity-0 transition hover:bg-hover group-hover:opacity-70"
                onClick={(e) => {
                  e.stopPropagation();
                  closeTab(tab.key);
                }}
              >
                <X size={11} />
              </button>
            </div>
          ))}
        </div>
        <Tip label="新建终端">
          <Button size="icon-sm" onClick={() => newTab(cwd)}>
            <Plus size={14} />
          </Button>
        </Tip>
        {activeTab && (
          <Tip label="清屏">
            <Button size="icon-sm" onClick={() => clear(activeTab.key)}>
              <Eraser size={13} />
            </Button>
          </Tip>
        )}
      </div>
      {activeTab && (
        <div className="mono flex h-6 shrink-0 items-center truncate px-3 text-[11px] text-faint" title={activeTab.cwd}>
          {activeTab.cwd}
        </div>
      )}
      <div ref={box} className={clsx("ee-term-box min-h-0 flex-1 overflow-hidden pb-1 pl-3 pr-1", t.tabs.length === 0 && "hidden")} />
      {t.tabs.length === 0 && t.available?.ok && (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 pb-16 text-[12.5px] text-faint">
          <SquareTerminal size={26} className="opacity-60" />
          <Button onClick={() => newTab(cwd)}>
            <Plus size={13} /> 新建终端
          </Button>
        </div>
      )}
    </div>
  );
}
