// 斜杠命令弹出菜单（D3-B）。键盘选择由 Composer 处理，这里只负责显示。
import clsx from "clsx";
import { Slash } from "lucide-react";
import type { SlashItem } from "../lib/slash";

export function SlashMenu({ items, sel, onPick, onHover }: { items: SlashItem[]; sel: number; onPick: (it: SlashItem) => void; onHover: (i: number) => void }) {
  return (
    <div className="absolute bottom-full left-0 right-0 z-30 mb-2 max-h-[320px] overflow-y-auto rounded-xl border border-line bg-elev p-1 shadow-[var(--shadow)] fadein">
      <div className="px-2.5 pb-1 pt-1.5 text-[11.5px] text-faint">命令 · ↑↓ 选择 · Enter/Tab 确定 · Esc 关闭</div>
      {items.map((it, i) => (
        <button
          key={it.key}
          ref={(el) => {
            if (i === sel && el) el.scrollIntoView({ block: "nearest" });
          }}
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(it);
          }}
          onMouseEnter={() => onHover(i)}
          className={clsx(
            "flex w-full min-w-0 items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px]",
            i === sel ? "bg-hover text-fg" : "text-muted",
          )}
        >
          <Slash size={12} className="shrink-0 text-faint" />
          <span className="mono shrink-0">{it.label}</span>
          {it.badge && <span className="shrink-0 rounded border border-line px-1 text-[10.5px] text-faint">{it.badge}</span>}
          {it.hint && <span className="ml-auto truncate pl-3 text-[12px] text-faint">{it.hint}</span>}
        </button>
      ))}
    </div>
  );
}
