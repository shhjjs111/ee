import clsx from "clsx";
import { ArrowUp, AtSign, Cpu, File, Shield, ShieldCheck, ShieldOff, Square } from "lucide-react";
import React, { useEffect, useRef, useState } from "react";
import { useApp } from "../lib/store";
import { Select, Spinner, Tip } from "./ui";

const MODES = [
  { value: "read-only", label: "只读", hint: "只能读文件和运行只读命令", icon: <Shield size={14} /> },
  { value: "default", label: "默认", hint: "工作区内改文件免问；其他命令先问你", icon: <ShieldCheck size={14} /> },
  { value: "auto", label: "全自动", hint: "都不问（deny 规则和危险命令仍会拦）", icon: <ShieldOff size={14} /> },
];

export function modeLabel(m: string) {
  return MODES.find((x) => x.value === m)?.label ?? m;
}

function ContextRing({ left, onCompact, compacting }: { left: number | null; onCompact: () => void; compacting: boolean }) {
  if (compacting) {
    return (
      <span className="flex h-7 items-center gap-1.5 px-1.5 text-[12px] text-muted">
        <Spinner size={12} /> 正在压缩上下文…
      </span>
    );
  }
  if (left === null || left === undefined) return null;
  const used = 1 - left;
  if (used < 0.05) return null; // 用得很少时不显示，免得像加载圈
  const r = 7;
  const c = 2 * Math.PI * r;
  return (
    <Tip label={`上下文剩余 ${Math.round(left * 100)}% · 点击压缩（/compact）`}>
      <button onClick={onCompact} className="flex h-7 w-7 items-center justify-center rounded-lg hover:bg-hover">
      <svg width="18" height="18" viewBox="0 0 18 18" className="text-faint">
        <circle cx="9" cy="9" r={r} fill="none" stroke="currentColor" strokeOpacity="0.3" strokeWidth="2" />
        <circle
          cx="9"
          cy="9"
          r={r}
          fill="none"
          stroke={used > 0.85 ? "var(--danger)" : "var(--fg-muted)"}
          strokeWidth="2"
          strokeDasharray={`${c * used} ${c}`}
          transform="rotate(-90 9 9)"
          strokeLinecap="round"
        />
      </svg>
      </button>
    </Tip>
  );
}

export function Composer({ autoFocus }: { autoFocus?: boolean }) {
  const s = useApp();
  const detail = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const running = !!detail && detail.status !== "idle";
  const [text, setText] = useState("");
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const [sel, setSel] = useState(0);
  const ta = useRef<HTMLTextAreaElement>(null);
  const cwd = detail?.cwd ?? s.activeWorkspace;
  const model = s.draftModel ?? detail?.model ?? s.defaultModel;
  const mode = s.draftMode;

  useEffect(() => {
    if (autoFocus) ta.current?.focus();
  }, [autoFocus, s.activeThreadId]);

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = Math.min(260, Math.max(24, el.scrollHeight)) + "px";
  }, [text]);

  useEffect(() => {
    if (!mention || !cwd || !s.rpc) return;
    let alive = true;
    s.rpc
      .call("files/search", { cwd, query: mention.query, limit: 12 })
      .then((r) => {
        if (alive) {
          setFiles(r.files);
          setSel(0);
        }
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [mention?.query, cwd]);

  function updateMention(value: string, caret: number) {
    const before = value.slice(0, caret);
    const m = /(^|\s)@([^\s@]*)$/.exec(before);
    setMention(m ? { start: caret - m[2].length - 1, query: m[2] } : null);
  }

  function insertFile(f: string) {
    if (!mention) return;
    const caret = ta.current?.selectionStart ?? text.length;
    const next = text.slice(0, mention.start) + "@" + f + " " + text.slice(caret);
    setText(next);
    setMention(null);
    requestAnimationFrame(() => {
      const pos = mention.start + f.length + 2;
      ta.current?.setSelectionRange(pos, pos);
      ta.current?.focus();
    });
  }

  function submit() {
    const t = text.trim();
    if (!t) return;
    if (t === "/compact") {
      setText("");
      setMention(null);
      s.compact();
      return;
    }
    s.send(t);
    setText("");
    setMention(null);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return; // 中文输入法组字中
    if (mention && files.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSel((sel + 1) % files.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSel((sel - 1 + files.length) % files.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        insertFile(files[sel]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMention(null);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape" && running) {
      s.interrupt();
    }
  }

  const modelOptions = s.models.map((m) => ({
    value: m.name,
    label: m.name,
    hint: `${m.provider} · ${m.model}${m.has_key ? "" : " · 未配置 key"}`,
  }));
  if (model && !modelOptions.some((o) => o.value === model)) modelOptions.unshift({ value: model, label: model, hint: "" });

  return (
    <div className="relative">
      {mention && files.length > 0 && (
        <div className="absolute bottom-full left-0 right-0 z-30 mb-2 overflow-hidden rounded-xl border border-line bg-elev p-1 shadow-[var(--shadow)] fadein">
          <div className="px-2.5 pb-1 pt-1.5 text-[11.5px] text-faint">文件</div>
          {files.map((f, i) => (
            <button
              key={f}
              onMouseDown={(e) => {
                e.preventDefault();
                insertFile(f);
              }}
              onMouseEnter={() => setSel(i)}
              className={clsx(
                "flex w-full min-w-0 items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px]",
                i === sel ? "bg-hover text-fg" : "text-muted",
              )}
            >
              <File size={13} className="shrink-0 text-faint" />
              <span className="truncate">{f.split("/").pop()}</span>
              <span className="mono ml-auto truncate pl-3 text-[11.5px] text-faint">{f}</span>
            </button>
          ))}
        </div>
      )}
      <div
        className={clsx(
          "rounded-[20px] border bg-elev shadow-[var(--shadow)] transition-colors",
          "border-line focus-within:border-line-strong",
        )}
      >
        <textarea
          ref={ta}
          value={text}
          rows={1}
          spellCheck={false}
          placeholder={running ? "继续输入，会在当前步骤结束后送给 ee…" : "让 ee 帮你写代码、改 bug、解释项目…  输入 @ 引用文件"}
          onChange={(e) => {
            setText(e.target.value);
            updateMention(e.target.value, e.target.selectionStart);
          }}
          onClick={(e) => updateMention(text, (e.target as HTMLTextAreaElement).selectionStart)}
          onKeyDown={onKeyDown}
          className="block max-h-[260px] w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-[14px] leading-[1.6] text-fg outline-none placeholder:text-faint"
        />
        <div className="flex items-center gap-1 px-2 pb-2 pt-1">
          <Tip label="引用文件（@）">
            <button
              className="flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg"
              onClick={() => {
                const next = text + (text && !text.endsWith(" ") ? " @" : "@");
                setText(next);
                setMention({ start: next.length - 1, query: "" });
                ta.current?.focus();
              }}
            >
              <AtSign size={15} />
            </button>
          </Tip>
          <Select
            value={model}
            options={modelOptions}
            onChange={(v) => s.set({ draftModel: v })}
            icon={<Cpu size={13} />}
            title="模型"
          />
          <Select
            value={mode}
            options={MODES}
            onChange={(v) => {
              localStorage.setItem("ee.mode", v);
              s.set({ draftMode: v });
            }}
            icon={MODES.find((m) => m.value === mode)?.icon}
            title="权限模式"
          />
          <div className="flex-1" />
          <ContextRing
            left={detail?.context_left ?? null}
            compacting={detail?.status === "compacting"}
            onCompact={() => s.compact()}
          />
          {detail?.status === "compacting" ? null : running ? (
            <Tip label="停止（Esc）">
              <button
                onClick={() => s.interrupt()}
                className="ml-1 flex h-8 w-8 items-center justify-center rounded-full bg-fg text-bg transition hover:opacity-85"
              >
                <Square size={12} fill="currentColor" />
              </button>
            </Tip>
          ) : (
            <Tip label="发送（Enter）· 换行（Shift+Enter）">
              <button
                onClick={submit}
                disabled={!text.trim()}
                className="ml-1 flex h-8 w-8 items-center justify-center rounded-full bg-fg text-bg transition hover:opacity-85 disabled:opacity-25"
              >
                <ArrowUp size={16} strokeWidth={2.4} />
              </button>
            </Tip>
          )}
        </div>
      </div>
    </div>
  );
}
