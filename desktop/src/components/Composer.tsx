import { reportError } from "../lib/errors";
import clsx from "clsx";
import { BgCommands } from "./BgCommands";
import { ContextRing } from "./ContextPanel";
import { GitBranch, ArrowUp, AtSign, Cpu, File, ListChecks, Shield, ShieldCheck, ShieldOff, SlidersHorizontal, Square, X } from "lucide-react";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { isImageFile, MAX_ATTACHMENTS, prepareImage, type Attachment } from "../lib/attachments";
import {
  effortLabel,
  effortOptionsFor,
  effortSupported,
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
import { GoalMenu } from "./GoalMenu";
import { SlashMenu } from "./SlashMenu";
import { mergeDraft, queueKindForKey, splitQueued, type QueueKind } from "../lib/queue";
import { QueueSendButton, QueueTray } from "./QueueTray";
import { LoopTray } from "./LoopTray";
import { LOOP_HELP, loopListText, loopToAutomation, parseLoopArgs, type SessionLoop } from "../lib/loop";
import { useLoops } from "../lib/loopStore";
import { useAutomations } from "../lib/automationStore";
import { Select, Spinner, Tip, Menu } from "./ui";

const MODES = [
  { value: "read-only", label: "只读", hint: "只能读文件和运行只读命令", icon: <Shield size={14} /> },
  { value: "default", label: "默认", hint: "工作区内改文件免问；其他命令先问你", icon: <ShieldCheck size={14} /> },
  { value: "auto", label: "全自动", hint: "都不问（deny 规则和危险命令仍会拦）", icon: <ShieldOff size={14} /> },
  { value: "plan", label: "计划", hint: "只调研出方案，你批准后再动手（Shift+Tab 切换模式）", icon: <ListChecks size={14} /> },
];

export function modeLabel(m: string) {
  return MODES.find((x) => x.value === m)?.label ?? m;
}

export function Composer({ autoFocus }: { autoFocus?: boolean }) {
  const s = useApp();
  const detail = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const running = !!detail && detail.status !== "idle";
  const queued = useMemo(() => (detail ? splitQueued(detail.items).queued : []), [detail?.items]);
  const allLoops = useLoops((st) => st.loops);
  const loops = useMemo(() => allLoops.filter((l) => l.thread_id === s.activeThreadId), [allLoops, s.activeThreadId]);
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
    s.rpc.call("styles/list").then((r) => alive && setStyles(r.styles ?? [])).catch(reportError("读取回答风格列表失败"));
    s.rpc.call("reasoning/prefs").then((r) => alive && setRemembered(r.remembered ?? {})).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [s.rpc, s.activeThreadId, detail?.effort]);
  const effort = (detail ? detail.effort : s.draftEffort) ?? remembered[model] ?? s.defaultEffort ?? "";
  const style = (detail ? detail.style : s.draftStyle) ?? s.defaultStyle ?? "default";
  // 草稿里换了模型时按草稿模型的能力显示（DeepSeek：默认（high）/ 低 / 最高（max）/ 关闭思考）
  const support =
    (s.draftModel && detail?.model !== s.draftModel ? undefined : detail?.effort_support) ??
    s.models.find((m) => m.name === model)?.effort_support ?? { effort: false, off: false };

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

  // 中断 / 撤回退回的排队消息放回输入框（排在正在写的文字前面）
  useEffect(() => {
    const r = s.returnedDraft;
    if (!r || r.threadId !== s.activeThreadId) return;
    s.set({ returnedDraft: null });
    setText((cur) => mergeDraft(r.texts, cur));
    requestAnimationFrame(() => ta.current?.focus());
  }, [s.returnedDraft?.nonce, s.activeThreadId]);

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
      .catch((e) => alive && reportError("搜索文件失败")(e));
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
      .catch((e) => alive && reportError("读取自定义提示列表失败")(e));
    s.rpc
      .call("skills/list", { cwd })
      .then((r) => alive && setSkills(r.skills ?? []))
      .catch((e) => alive && reportError("读取技能列表失败")(e));
    return () => {
      alive = false;
    };
  }, [slashing, cwd, s.rpc]);

  const modelHints = s.models.map((m) => ({ name: m.name, hint: `${m.provider} · ${m.model}` }));
  const slashList = slashClosed ? null : slashItems(text, prompts, modelHints, skills, styles, support);
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
          s.showToast(`用法：/effort ${effortOptionsFor(support).map((o) => o.label).join(" | ")}`, "error");
          return true;
        }
        clearInput();
        // DeepSeek 没有 中 / 高 档：后端会规整成 默认 / 最高，这里提示用后端回来的档位
        const unsupported = !effortSupported(v, support) && !(support.effort && ["medium", "high"].includes(v));
        if (await s.setOptions({ effort: v }))
          s.showToast(`推理强度：${effortLabel(v, support)}` + (unsupported ? "（当前模型不支持，请求里不会发送）" : ""));
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
      case "loop":
        await runLoop(args);
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

  /** /loop：会话内定时重复发送（D5-3）。 */
  async function runLoop(args: string) {
    const cmd = parseLoopArgs(args);
    if (cmd.kind === "help") {
      s.showToast(LOOP_HELP);
      return;
    }
    if (cmd.kind === "list") {
      clearInput();
      s.showToast(loopListText(loops));
      return;
    }
    if (cmd.kind === "stop") {
      if (!s.activeThreadId) {
        s.showToast("这个会话没有循环", "error");
        return;
      }
      try {
        const r = await s.rpc!.call("loop/stop", { thread_id: s.activeThreadId, id: cmd.id });
        clearInput();
        useLoops.getState().drop(r.stopped ?? []);
        s.showToast(`已停止循环 ${(r.stopped ?? []).map((x: string) => "#" + x).join("、")}`);
      } catch (e) {
        s.showToast((e as Error).message, "error");
      }
      return;
    }
    try {
      const id = await s.ensureThread();
      if (!id) return;
      const r = await s.rpc!.call("loop/create", { thread_id: id, text: cmd.text });
      clearInput();
      const lp = r.loop as SessionLoop;
      useLoops.getState().upsert(lp);
      s.showToast(`已创建循环 #${lp.id}：${lp.schedule_text}${lp.note ? `（${lp.note}）` : ""}。关闭 ee 后不保留，可在输入框上方转为自动化`);
    } catch (e) {
      s.showToast((e as Error).message, "error");
    }
  }

  async function stopLoop(id: string) {
    if (!s.activeThreadId) return;
    try {
      const r = await s.rpc!.call("loop/stop", { thread_id: s.activeThreadId, id });
      useLoops.getState().drop(r.stopped ?? []);
    } catch (e) {
      s.showToast((e as Error).message, "error");
    }
  }

  function convertLoop(lp: SessionLoop) {
    const ws = detail?.worktree?.repo ?? detail?.workspace ?? detail?.cwd ?? s.activeWorkspace ?? "";
    useAutomations.getState().set({ editing: loopToAutomation(lp, ws, detail?.model ?? "") });
    s.set({ view: "automations" });
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

  async function submit(queue?: QueueKind) {
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
    s.send(t, undefined, running ? (queue ?? "steer") : undefined);
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
      submit(running ? queueKindForKey(e) : undefined); // 运行中：Enter 插话，Ctrl+Enter 完成后再做
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
        <LoopTray loops={loops} onStop={stopLoop} onConvert={convertLoop} />
        <QueueTray items={queued} onWithdraw={(id) => s.withdrawQueued(id)} />
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
          placeholder={running ? "继续输入：Enter 插话（当前步骤结束后送入），Ctrl+Enter 等这一轮做完再做…" : "让 ee 帮你写代码、改 bug、解释项目…  输入 @ 引用文件，/ 使用命令"}
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
          // overflow-y-auto + 自适应高度：行高 1.6 与 scrollHeight 取整差 1-2px 时，
          // 浏览器会判定"内容溢出"并常显一条竖直滚动条（实测 offsetWidth 718 / clientWidth 708）。
          // 单行不需要滚动，交给 max-h 兜住多行情况即可。
          className="block max-h-[260px] w-full resize-none overflow-y-hidden bg-transparent px-4 pb-1 pt-3.5 text-[14px] leading-[1.6] text-fg outline-none placeholder:text-faint"
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
          {/* Codex 式的 `+` 菜单：文件/图片 + 目标，两项同在一组。
              图片仍走原来的 fileInput，只是入口收进了菜单里。 */}
          <GoalMenu
            onPickFiles={() => fileInput.current?.click()}
            onSubmit={(req) => {
              const t = text.trim();
              if (!t) {
                s.showToast("先写一句要做什么，再设定目标", "error");
                return;
              }
              setText("");
              void s.sendGoal(t, req.command, req.maxRounds);
            }}
            disabled={running || attBusy}
          />
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
          {/* 推理强度与回答风格收进「更多」：两者都是低频、非每次发送都要看的参数。
              权限模式、模型与发送留在原位——那是每次都要确认的。
              控件本身还是原来的 Select，动作与 RPC 参数一字未改。 */}
          <Menu
            width="min-w-[180px]"
            trigger={
              <button
                className="no-drag flex h-7 items-center gap-1 rounded-lg px-2 text-[12.5px] text-muted transition-colors hover:bg-hover hover:text-fg"
                title="更多参数：推理强度、回答风格"
                data-testid="composer-more"
              >
                <SlidersHorizontal size={14} />
                <span className="hidden sm:inline">更多</span>
              </button>
            }
            sections={[
              {
                group: "推理强度",
                items: effortOptionsFor(support).map((o) => ({
                  label: o.label,
                  hint: o.value === effort ? "当前" : undefined,
                  disabled: !effortSupported(o.value, support),
                  onSelect: () => void s.setOptions({ effort: o.value }),
                })),
              },
              {
                group: "回答风格",
                items: (styles.length ? styles : [{ id: "default", label: "默认", description: "" } as StyleInfo]).map((x) => ({
                  label: x.label,
                  desc: x.description || (x.id === "default" ? "常规回答" : undefined),
                  hint: x.id === style ? "当前" : undefined,
                  onSelect: () => void s.setOptions({ style: x.id }),
                })),
              },
            ]}
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
            threadId={s.activeThreadId}
            left={detail?.context_left ?? null}
            compacting={detail?.status === "compacting"}
            canCompact={!running}
            onCompact={() => s.compact()}
          />
          {running && detail?.status !== "compacting" && text.trim() && !text.trim().startsWith("/") && (
            <QueueSendButton disabled={attBusy} onSend={(k) => submit(k)} />
          )}
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
                onClick={() => submit()}
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
