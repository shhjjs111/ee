import clsx from "clsx";
import { BgCommands } from "./BgCommands";
import { GitBranch, ArrowUp, AtSign, Brain, Cpu, File, GraduationCap, ImagePlus, ListChecks, Shield, ShieldCheck, ShieldOff, Square, X } from "lucide-react";
import React, { useEffect, useRef, useState } from "react";
import { isImageFile, MAX_ATTACHMENTS, prepareImage, type Attachment } from "../lib/attachments";
import {
  EFFORT_OPTIONS,
  effortLabel,
  isSkillCommand,
  parseSlash,
  resolveEffort,
  resolveMode,
  resolveStyle,
  slashItems,
  type PromptInfo,
  type SkillInfo,
  type SlashItem,
} from "../lib/slash";
import type { StyleInfo } from "../types";
import { useApp } from "../lib/store";
import { SlashMenu } from "./SlashMenu";
import { Select, Spinner, Tip } from "./ui";

const MODES = [
  { value: "read-only", label: "只读", hint: "只能读文件和运行只读命令", icon: <Shield size={14} /> },
  { value: "default", label: "默认", hint: "工作区内改文件免问；其他命令先问你", icon: <ShieldCheck size={14} /> },
  { value: "auto", label: "全自动", hint: "都不问（deny 规则和危险命令仍会拦）", icon: <ShieldOff size={14} /> },
  { value: "plan", label: "计划", hint: "只调研出方案，你批准后再动手（Shift+Tab 切换模式）", icon: <ListChecks size={14} /> },
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
  const [prompts, setPrompts] = useState<PromptInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [slashSel, setSlashSel] = useState(0);
  const [slashClosed, setSlashClosed] = useState(false);
  const [atts, setAtts] = useState<Attachment[]>([]);
  const [attBusy, setAttBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const ta = useRef<HTMLTextAreaElement>(null);
  const cwd = detail?.cwd ?? s.activeWorkspace;
  const model = s.draftModel ?? detail?.model ?? s.defaultModel;
  const mode = s.draftMode;
  // D5-9：推理强度 / 回答风格（有对话时取会话的；新对话取草稿，否则取按模型记住的 / 设置里的默认）
  const [styles, setStyles] = useState<StyleInfo[]>([]);
  const [remembered, setRemembered] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!s.rpc) return;
    let alive = true;
    s.rpc.call("styles/list").then((r) => alive && setStyles(r.styles ?? [])).catch(() => undefined);
    s.rpc.call("reasoning/prefs").then((r) => alive && setRemembered(r.remembered ?? {})).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [s.rpc, s.activeThreadId, detail?.effort]);
  const effort = (detail ? detail.effort : s.draftEffort) ?? remembered[model] ?? s.defaultEffort ?? "";
  const style = (detail ? detail.style : s.draftStyle) ?? s.defaultStyle ?? "default";
  const support = detail?.effort_support ?? s.models.find((m) => m.name === model)?.effort_support ?? { effort: false, off: false };

  useEffect(() => {
    if (autoFocus) ta.current?.focus();
  }, [autoFocus, s.activeThreadId]);

  // 分叉后把原消息预填进输入框
  useEffect(() => {
    if (!s.composerDraft) return;
    setText(s.composerDraft.text);
    s.set({ composerDraft: null });
    requestAnimationFrame(() => {
      const n = ta.current?.value.length ?? 0;
      ta.current?.setSelectionRange(n, n);
      ta.current?.focus();
    });
  }, [s.composerDraft?.nonce]);

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

  // 输入 "/" 时拉一次自定义提示列表（切换项目后重新拉）
  const slashing = text.startsWith("/");
  useEffect(() => {
    if (!slashing || !s.rpc) return;
    let alive = true;
    s.rpc
      .call("prompts/list", { cwd })
      .then((r) => alive && setPrompts(r.prompts))
      .catch(() => undefined);
    s.rpc
      .call("skills/list", { cwd })
      .then((r) => alive && setSkills(r.skills ?? []))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [slashing, cwd, s.rpc]);

  const modelHints = s.models.map((m) => ({ name: m.name, hint: `${m.provider} · ${m.model}` }));
  const slashList = slashClosed ? null : slashItems(text, prompts, modelHints, skills, styles);
  const showSlash = !!slashList && slashList.length > 0 && !mention;
  useEffect(() => setSlashSel(0), [slashList?.length, text.split(" ")[0]]);

  function clearInput() {
    setText("");
    setMention(null);
    setSlashClosed(false);
    setAtts([]);
  }

  async function addFiles(list: FileList | File[]) {
    const files = Array.from(list).filter(isImageFile);
    if (!files.length) return;
    const room = MAX_ATTACHMENTS - atts.length;
    if (room <= 0) {
      s.showToast(`一条消息最多 ${MAX_ATTACHMENTS} 张图片`, "error");
      return;
    }
    setAttBusy(true);
    try {
      const out: Attachment[] = [];
      for (const f of files.slice(0, room)) {
        try {
          out.push(await prepareImage(f));
        } catch (e) {
          s.showToast(`${f.name}：${(e as Error).message}`, "error");
        }
      }
      setAtts((cur) => [...cur, ...out].slice(0, MAX_ATTACHMENTS));
      if (files.length > room) s.showToast(`一条消息最多 ${MAX_ATTACHMENTS} 张图片，多出的没有加入`, "error");
    } finally {
      setAttBusy(false);
    }
  }

  /** 执行斜杠命令；返回 false 表示不是已知命令（当普通消息发送）。 */
  async function runSlash(t: string): Promise<boolean> {
    const p = parseSlash(t);
    if (!p) return false;
    const { name, args } = p;
    switch (name) {
      case "compact":
        clearInput();
        s.compact();
        return true;
      case "new":
        clearInput();
        s.newThread();
        return true;
      case "undo":
        clearInput();
        s.undo();
        return true;
      case "model": {
        const hit = s.models.find((m) => m.name === args) ?? s.models.find((m) => m.name.toLowerCase().includes(args.toLowerCase()));
        if (!args || !hit) {
          s.showToast(args ? `没有这个模型：${args}` : "用法：/model 模型名", "error");
          return true;
        }
        clearInput();
        s.set({ draftModel: hit.name });
        s.showToast(`已切换模型：${hit.name}`);
        return true;
      }
      case "mode": {
        const m = resolveMode(args);
        if (!m) {
          s.showToast("用法：/mode 只读 | 默认 | 全自动", "error");
          return true;
        }
        clearInput();
        localStorage.setItem("ee.mode", m);
        s.set({ draftMode: m });
        s.showToast(`权限模式：${modeLabel(m)}`);
        return true;
      }
      case "effort": {
        const v = resolveEffort(args);
        if (v === null || !args) {
          s.showToast("用法：/effort 默认 | 低 | 中 | 高 | 关闭", "error");
          return true;
        }
        clearInput();
        const unsupported = (v === "off" && !support.off) || (["low", "medium", "high"].includes(v) && !support.effort);
        if (await s.setOptions({ effort: v }))
          s.showToast(`推理强度：${effortLabel(v)}` + (unsupported ? "（当前模型不支持，请求里不会发送）" : ""));
        return true;
      }
      case "style": {
        const id = resolveStyle(args, styles);
        if (!id) {
          s.showToast(`用法：/style ${styles.map((x) => x.label).join(" | ") || "默认 | 简洁"}`, "error");
          return true;
        }
        clearInput();
        if (await s.setOptions({ style: id })) s.showToast(`回答风格：${styles.find((x) => x.id === id)?.label ?? id}`);
        return true;
      }
      case "memory":
        clearInput();
        if (!args) {
          s.openSettings("tools");
          return true;
        }
        try {
          await s.rpc!.call("memory/add", { text: args, scope: "user", cwd });
          s.showToast("已记住，新对话开始时生效");
        } catch (e) {
          s.showToast((e as Error).message, "error");
        }
        return true;
      case "mcp":
        clearInput();
        s.openSettings("mcp");
        return true;
      case "plan":
        clearInput();
        localStorage.setItem("ee.mode", "plan");
        s.set({ draftMode: "plan" });
        if (args) s.send(args);
        else s.showToast("已切到计划模式：先出方案，批准后再改文件");
        return true;
      case "review":
        clearInput();
        s.set({ dialog: { kind: "review", instructions: args } });
        return true;
      case "commit":
        clearInput();
        s.set({ dialog: { kind: "commit" } });
        return true;
    }
    // 技能：/名字 原样发送，后端把 SKILL.md 全文附在消息前
    if (isSkillCommand(name, prompts, skills)) return false;
    if (!prompts.some((x) => x.name === name)) {
      // 可能还没拉到列表：再问一次后端
      try {
        const r = await s.rpc!.call("prompts/list", { cwd });
        setPrompts(r.prompts);
        if (!r.prompts.some((x: PromptInfo) => x.name === name)) return false;
      } catch {
        return false;
      }
    }
    try {
      const r = await s.rpc!.call("prompts/expand", { name, args, cwd });
      clearInput();
      s.send(r.text);
    } catch (e) {
      s.showToast((e as Error).message, "error");
    }
    return true;
  }

  function pickSlash(it: SlashItem) {
    if ("fill" in it.action) {
      setText(it.action.fill);
      requestAnimationFrame(() => {
        const n = (it.action as { fill: string }).fill.length;
        ta.current?.setSelectionRange(n, n);
        ta.current?.focus();
      });
    } else {
      runSlash(it.action.run);
    }
  }

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

  async function submit() {
    const t = text.trim();
    if (!t && !atts.length) return;
    if (attBusy) return;
    if (!atts.length && t.startsWith("/") && (await runSlash(t))) return;
    if (atts.length) {
      if (running) {
        s.showToast("ee 正在工作，等这一回合结束再发图片", "error");
        return;
      }
      const sending = atts;
      setText("");
      setAtts([]);
      const ok = await s.send(t, sending);
      if (!ok) {
        setText(t);
        setAtts(sending);
      }
      return;
    }
    s.send(t);
    clearInput();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return; // 中文输入法组字中
    if (showSlash && slashList) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashSel((slashSel + 1) % slashList.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashSel((slashSel - 1 + slashList.length) % slashList.length);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        const it = slashList[Math.min(slashSel, slashList.length - 1)];
        // 已经完整输入了一个不带参数的命令，Enter 直接执行
        pickSlash(it);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setSlashClosed(true);
        return;
      }
    }
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
    if (e.key === "Tab" && e.shiftKey) {
      // Shift+Tab 轮换权限模式：默认 → 全自动 → 计划 → 只读 → 默认
      e.preventDefault();
      const order = ["default", "auto", "plan", "read-only"];
      const next = order[(order.indexOf(s.draftMode) + 1) % order.length];
      localStorage.setItem("ee.mode", next);
      s.set({ draftMode: next });
      s.showToast(`权限模式：${modeLabel(next)}`);
      return;
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
      {showSlash && slashList && <SlashMenu items={slashList} sel={slashSel} onPick={pickSlash} onHover={setSlashSel} />}
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
          dragOver ? "border-accent" : "border-line focus-within:border-line-strong",
        )}
        onDragOver={(e) => {
          if (Array.from(e.dataTransfer.items).some((i) => i.kind === "file")) {
            e.preventDefault();
            setDragOver(true);
          }
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          setDragOver(false);
          if (e.dataTransfer.files.length) {
            e.preventDefault();
            addFiles(e.dataTransfer.files);
          }
        }}
      >
        {(atts.length > 0 || attBusy) && (
          <div className="flex flex-wrap items-center gap-2 px-3 pt-3">
            {atts.map((a) => (
              <div key={a.key} className="group/att relative h-14 w-14 overflow-hidden rounded-lg border border-line bg-soft">
                <img src={a.thumb} alt={a.name} title={a.name} className="h-full w-full object-cover" />
                <button
                  onClick={() => setAtts((cur) => cur.filter((x) => x.key !== a.key))}
                  className="absolute right-0.5 top-0.5 hidden h-5 w-5 items-center justify-center rounded-full bg-black/60 text-white group-hover/att:flex"
                  title="移除"
                >
                  <X size={11} />
                </button>
              </div>
            ))}
            {attBusy && <Spinner size={14} className="text-muted" />}
            {atts.length > 0 && s.models.find((m) => m.name === model)?.vision === false && (
              <span className="text-[11.5px] text-warn">当前模型不支持看图，图片不会发给模型</span>
            )}
          </div>
        )}
        <textarea
          ref={ta}
          value={text}
          rows={1}
          spellCheck={false}
          placeholder={running ? "继续输入，会在当前步骤结束后送给 ee…" : "让 ee 帮你写代码、改 bug、解释项目…  输入 @ 引用文件，/ 使用命令"}
          onChange={(e) => {
            setText(e.target.value);
            if (!e.target.value.startsWith("/")) setSlashClosed(false);
            updateMention(e.target.value, e.target.selectionStart);
          }}
          onClick={(e) => updateMention(text, (e.target as HTMLTextAreaElement).selectionStart)}
          onKeyDown={onKeyDown}
          onPaste={(e) => {
            const files = Array.from(e.clipboardData.files).filter(isImageFile);
            if (files.length) {
              e.preventDefault();
              addFiles(files);
            }
          }}
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
          <Tip label="添加图片（也可以粘贴或拖进来）">
            <button
              className="flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg"
              onClick={() => fileInput.current?.click()}
            >
              <ImagePlus size={15} />
            </button>
          </Tip>
          <input
            ref={fileInput}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />
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
          <Select
            value={effort}
            options={EFFORT_OPTIONS.map((o) => ({
              ...o,
              disabled: (o.value === "off" && !support.off) || (["low", "medium", "high"].includes(o.value) && !support.effort),
              hint:
                (o.value === "off" && !support.off) || (["low", "medium", "high"].includes(o.value) && !support.effort)
                  ? "当前模型不支持"
                  : o.hint,
            }))}
            onChange={(v) => s.setOptions({ effort: v })}
            icon={<Brain size={13} />}
            renderLabel={(o) => (o && o.value ? o.label : "推理")}
            title={support.effort || support.off ? "推理强度（本会话，按模型记住；/effort）" : "当前模型不支持调节推理强度"}
            className={clsx(!support.effort && !support.off && "opacity-50")}
          />
          <Select
            value={style}
            options={(styles.length ? styles : [{ id: "default", label: "默认", description: "" } as StyleInfo]).map((x) => ({
              value: x.id,
              label: x.label,
              hint: x.description || (x.id === "default" ? "常规回答" : undefined),
            }))}
            onChange={(v) => s.setOptions({ style: v })}
            icon={<GraduationCap size={13} />}
            renderLabel={(o) => (o && o.value !== "default" ? o.label : "风格")}
            title="回答风格（本会话；/style）"
          />
          {!s.activeThreadId && (
            <Tip label={s.draftWorktree ? "将在新的 git worktree（独立目录 + ee/ 分支）里开始，不动主目录；完成后在左栏项目菜单「Worktree」里合并或删除" : "在新的 git worktree 里开始（独立目录和分支，适合并行做多件事）"}>
              <button
                onClick={() => s.set({ draftWorktree: !s.draftWorktree })}
                className={clsx(
                  "flex h-7 items-center gap-1 rounded-full px-2 text-[12px] transition",
                  s.draftWorktree ? "bg-accent/15 text-accent" : "text-muted hover:bg-soft",
                )}
              >
                <GitBranch size={13} /> {s.draftWorktree ? "新 worktree" : "本地"}
              </button>
            </Tip>
          )}
          <div className="flex-1" />
          <BgCommands threadId={s.activeThreadId} />
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
                disabled={(!text.trim() && !atts.length) || attBusy}
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
