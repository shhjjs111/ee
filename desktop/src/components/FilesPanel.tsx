import { openPath } from "../lib/openPath";
import clsx from "clsx";
import {
  ArrowLeft,
  ChevronRight,
  Code2,
  Copy,
  Eye,
  File,
  FileCode2,
  FileImage,
  FileText,
  Folder,
  FolderOpen,
  FolderTree,
  RefreshCw,
  SquareArrowOutUpRight,
} from "lucide-react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { create } from "zustand";
import { highlight, langFromPath } from "../lib/highlight";
import { useApp } from "../lib/store";
import type { FileContent, TreeEntry } from "../types";
import { Markdown } from "./Markdown";
import { Button, Spinner, Tip } from "./ui";

// ------------------------------------------------------------ 树状态（按工作区保存，切标签不丢）
type TreeState = {
  cwd: string | null;
  children: Record<string, TreeEntry[] | "loading" | { error: string }>;
  expanded: Record<string, boolean>;
};
const useTree = create<TreeState>(() => ({ cwd: null, children: {}, expanded: {} }));

async function loadDir(cwd: string, path: string) {
  const rpc = useApp.getState().rpc;
  if (!rpc) return;
  const cur = useTree.getState().children[path];
  if (!cur) useTree.setState((s) => ({ children: { ...s.children, [path]: "loading" } }));
  try {
    const r = await rpc.call("files/tree", { cwd, path });
    if (useTree.getState().cwd !== cwd) return;
    useTree.setState((s) => ({ children: { ...s.children, [path]: r.entries } }));
  } catch (e) {
    if (useTree.getState().cwd !== cwd) return;
    useTree.setState((s) => ({ children: { ...s.children, [path]: { error: (e as Error).message } } }));
  }
}

function reloadAll(cwd: string) {
  const { expanded } = useTree.getState();
  loadDir(cwd, "");
  for (const [p, open] of Object.entries(expanded)) if (open) loadDir(cwd, p);
}

/** 打开某个文件时展开它的所有上级目录。 */
function revealPath(cwd: string, rel: string) {
  const parts = rel.split("/").filter(Boolean);
  const exp: Record<string, boolean> = {};
  for (let i = 1; i < parts.length; i++) exp[parts.slice(0, i).join("/")] = true;
  useTree.setState((s) => ({ expanded: { ...s.expanded, ...exp } }));
  for (const p of Object.keys(exp)) if (!useTree.getState().children[p]) loadDir(cwd, p);
}

// ------------------------------------------------------------ 图标
const IMG = /\.(png|jpe?g|gif|webp|bmp|ico|svg|avif)$/i;
const DOC = /\.(md|markdown|txt|rst|log)$/i;

function FileIcon({ name }: { name: string }) {
  if (IMG.test(name)) return <FileImage size={14} className="shrink-0 text-faint" />;
  if (DOC.test(name)) return <FileText size={14} className="shrink-0 text-faint" />;
  if (langFromPath(name)) return <FileCode2 size={14} className="shrink-0 text-faint" />;
  return <File size={14} className="shrink-0 text-faint" />;
}

function fmtSize(n?: number | null): string {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ------------------------------------------------------------ 树
function Dir({ cwd, path, depth, selected }: { cwd: string; path: string; depth: number; selected: string | null }) {
  const node = useTree((s) => s.children[path]);
  const expanded = useTree((s) => s.expanded);
  const s = useApp();
  if (node === undefined || node === "loading") {
    return (
      <div className="flex h-7 items-center gap-2 text-[12px] text-faint" style={{ paddingLeft: 12 + depth * 14 }}>
        <Spinner size={11} /> 加载中…
      </div>
    );
  }
  if (!Array.isArray(node)) {
    return (
      <div className="py-1 text-[12px] text-danger" style={{ paddingLeft: 12 + depth * 14 }}>
        {node.error}
      </div>
    );
  }
  if (node.length === 0 && depth > 0) {
    return (
      <div className="flex h-7 items-center text-[12px] text-faint" style={{ paddingLeft: 26 + depth * 14 }}>
        空文件夹
      </div>
    );
  }
  return (
    <>
      {node.map((e) => {
        const open = !!expanded[e.path];
        const isSel = selected === e.path;
        return (
          <div key={e.path}>
            <div
              className={clsx(
                "flex h-7 cursor-default items-center gap-1.5 pr-3 text-[13px]",
                isSel ? "bg-active text-fg" : "text-fg/90 hover:bg-hover",
                e.ignored && "opacity-50",
              )}
              style={{ paddingLeft: 8 + depth * 14 }}
              onClick={() => {
                if (e.type === "dir") {
                  useTree.setState((st) => ({ expanded: { ...st.expanded, [e.path]: !open } }));
                  if (!open && !useTree.getState().children[e.path]) loadDir(cwd, e.path);
                } else {
                  s.openFile(e.path);
                }
              }}
            >
              {e.type === "dir" ? (
                <>
                  <ChevronRight size={12} className={clsx("shrink-0 text-faint transition-transform", open && "rotate-90")} />
                  {open ? <FolderOpen size={14} className="shrink-0 text-faint" /> : <Folder size={14} className="shrink-0 text-faint" />}
                </>
              ) : (
                <>
                  <span className="w-3 shrink-0" />
                  <FileIcon name={e.name} />
                </>
              )}
              <span className="min-w-0 truncate">{e.name}</span>
            </div>
            {e.type === "dir" && open && <Dir cwd={cwd} path={e.path} depth={depth + 1} selected={selected} />}
          </div>
        );
      })}
    </>
  );
}

// ------------------------------------------------------------ 预览
const CodeView = memo(function CodeView({ text, path }: { text: string; path: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const lines = useMemo(() => text.replace(/\n$/, "").split("\n"), [text]);
  const lang = langFromPath(path);
  useEffect(() => {
    setHtml(null);
    if (!lang || text.length > 300_000 || lines.length > 8000) return;
    let alive = true;
    highlight(text.replace(/\n$/, ""), lang)
      .then((h) => alive && setHtml(h))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [text, lang]);
  const lnw = `${Math.max(2, String(lines.length).length)}ch`;
  return (
    <div className="file-code min-w-max text-[12.5px]" style={{ ["--ln-w" as any]: lnw }}>
      {html ? (
        <div dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <pre className="shiki mono">
          <code>
            {lines.map((l, i) => (
              <span key={i} className="line">
                {l}
                {"\n"}
              </span>
            ))}
          </code>
        </pre>
      )}
    </div>
  );
});

function Preview({ cwd, path, onBack }: { cwd: string; path: string; onBack: () => void }) {
  const s = useApp();
  const [data, setData] = useState<FileContent | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [source, setSource] = useState(false);

  const gen = useRef(0); // agent 改文件时会连续刷新：只认最后一次请求的结果
  const load = useCallback(async () => {
    if (!s.rpc) return;
    const tok = ++gen.current;
    setLoading(true);
    setErr(null);
    try {
      const r = await s.rpc.call("files/read", { cwd, path });
      if (tok === gen.current) setData(r);
    } catch (e) {
      if (tok !== gen.current) return;
      setData(null);
      setErr((e as Error).message);
    } finally {
      if (tok === gen.current) setLoading(false);
    }
  }, [cwd, path, s.rpc]);

  useEffect(() => {
    setSource(false);
    load();
  }, [load]);
  // agent 改了文件就刷新
  useEffect(() => {
    if (s.reviewNonce) load();
  }, [s.reviewNonce]);

  const rel = data?.path ?? path;
  const parts = rel.split("/").filter(Boolean);
  const doc = (data as any)?.doc as string | undefined; // PDF/Word/Excel/PPT 转出来的文字（D4-7）
  const isMd = /\.(md|markdown)$/i.test(rel) || doc === "docx" || doc === "pptx";
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-9 shrink-0 items-center gap-1 border-b border-line pl-1.5 pr-2">
        <Tip label="返回文件树">
          <Button size="icon-sm" onClick={onBack}>
            <ArrowLeft size={14} />
          </Button>
        </Tip>
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden text-[12.5px]" title={data?.abs ?? path}>
          {parts.slice(0, -1).length > 0 && (
            <span className="min-w-0 truncate text-faint">{parts.slice(0, -1).join(" / ")} /</span>
          )}
          <span className="shrink-0 font-medium text-fg">{parts[parts.length - 1] ?? path}</span>
        </div>
        {loading && <Spinner size={12} className="mr-1" />}
        {data && <span className="mr-1 shrink-0 text-[11.5px] text-faint">{fmtSize(data.size)}</span>}
        {isMd && data?.kind === "text" && (
          <Tip label={source ? "渲染预览" : "查看源码"}>
            <Button size="icon-sm" onClick={() => setSource(!source)}>
              {source ? <Eye size={14} /> : <Code2 size={14} />}
            </Button>
          </Tip>
        )}
        <Tip label="复制路径">
          <Button
            size="icon-sm"
            onClick={() => {
              navigator.clipboard?.writeText(rel);
              s.showToast("已复制路径");
            }}
          >
            <Copy size={13} />
          </Button>
        </Tip>
        {window.ee && data && (
          <Tip label="用默认程序打开">
            <Button size="icon-sm" onClick={() => openPath(data.abs)}>
              <SquareArrowOutUpRight size={13} />
            </Button>
          </Tip>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {err && <div className="px-5 py-8 text-center text-[12.5px] text-danger">{err}</div>}
        {data?.kind === "text" && (
          <>
            {doc && (
              <div className="border-b border-line bg-soft px-3 py-1.5 text-[11.5px] text-muted">
                {{ pdf: "PDF", docx: "Word", xlsx: "Excel", pptx: "PowerPoint" }[doc] ?? doc} 转换出的文字（只看内容，不含排版和图片）
              </div>
            )}
            {data.truncated && (
              <div className="border-b border-line bg-soft px-3 py-1.5 text-[11.5px] text-muted">
                文件较大，只显示前 {fmtSize(data.text?.length ?? 0)}
              </div>
            )}
            {isMd && !source ? (
              <div className="px-5 py-4">
                <Markdown text={data.text ?? ""} />
              </div>
            ) : data.text ? (
              <CodeView text={data.text} path={doc ? rel + ".txt" : rel} />
            ) : (
              <div className="px-5 py-8 text-center text-[12.5px] text-faint">空文件</div>
            )}
          </>
        )}
        {data?.kind === "image" && (
          <div className="flex min-h-full items-center justify-center p-5">
            <img
              src={data.data_url}
              alt={data.name}
              className="max-h-full max-w-full rounded-lg border border-line bg-[repeating-conic-gradient(var(--bg-soft)_0%_25%,transparent_0%_50%)] bg-[length:16px_16px] object-contain"
            />
          </div>
        )}
        {data?.kind === "binary" && (
          <div className="flex flex-col items-center px-6 py-14 text-center text-[12.5px] text-faint">
            <File size={28} className="mb-3 opacity-50" />
            {data.reason === "too_large" ? "文件太大，不预览" : data.reason === "doc_error" ? ((data as any).message ?? "无法解析") : "二进制文件，不预览"}
            {window.ee && (
              <Button className="mt-4" onClick={() => openPath(data.abs)}>
                <SquareArrowOutUpRight size={13} /> 用默认程序打开
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------ 面板
export function FilesPanel() {
  const s = useApp();
  const cwd = (s.activeThreadId ? s.details[s.activeThreadId]?.cwd : null) ?? s.activeWorkspace;
  const treeCwd = useTree((t) => t.cwd);
  const root = useTree((t) => t.children[""]);

  useEffect(() => {
    if (!cwd || !s.rpc) return;
    if (treeCwd !== cwd) {
      useTree.setState({ cwd, children: {}, expanded: {} });
      loadDir(cwd, "");
    }
  }, [cwd, s.rpc]);

  // 工具卡片里点了路径：展开到那个文件
  useEffect(() => {
    if (cwd && s.filePath && !/^([a-zA-Z]:)?[\\/]/.test(s.filePath)) revealPath(cwd, s.filePath);
  }, [s.filePath, s.fileNonce, cwd]);

  useEffect(() => {
    if (cwd && s.reviewNonce && useTree.getState().cwd === cwd) reloadAll(cwd);
  }, [s.reviewNonce]);

  if (!cwd) return <div className="p-6 text-center text-[13px] text-faint">先选择一个项目</div>;
  if (s.filePath) return <Preview key={s.filePath + s.fileNonce} cwd={cwd} path={s.filePath} onBack={() => s.set({ filePath: null })} />;

  const name = cwd.split(/[\\/]/).filter(Boolean).pop() || cwd;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line pl-3 pr-2 text-[12.5px] text-muted">
        <FolderTree size={13} />
        <span className="truncate font-medium text-fg" title={cwd}>
          {name}
        </span>
        <span className="flex-1" />
        <Tip label="刷新">
          <Button size="icon-sm" onClick={() => reloadAll(cwd)}>
            {root === "loading" ? <Spinner size={12} /> : <RefreshCw size={13} />}
          </Button>
        </Tip>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto py-1">
        {Array.isArray(root) && root.length === 0 ? (
          <div className="px-6 py-14 text-center text-[12.5px] text-faint">这个文件夹是空的</div>
        ) : (
          <Dir cwd={cwd} path="" depth={0} selected={s.lastFilePath} />
        )}
      </div>
    </div>
  );
}
