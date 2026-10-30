import { Check, Copy } from "lucide-react";
import React, { memo, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { highlight, normalizeLang } from "../lib/highlight";

export function CodeBlock({ code, lang, maxHeight }: { code: string; lang?: string | null; maxHeight?: number }) {
  const [html, setHtml] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let alive = true;
    const t = setTimeout(() => {
      highlight(code, lang).then((h) => alive && setHtml(h)).catch(() => undefined);
    }, 60); // 流式输出时节流
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [code, lang]);
  const label = normalizeLang(lang ?? undefined) ?? lang ?? "";
  return (
    <div className="group/code relative my-3 overflow-hidden rounded-xl border border-line bg-code">
      <div className="flex h-8 items-center justify-between border-b border-line px-3 text-[11.5px] text-faint">
        <span className="mono">{label || "text"}</span>
        <button
          className="flex items-center gap-1 rounded px-1.5 py-0.5 opacity-70 transition hover:bg-hover hover:opacity-100"
          onClick={() => {
            navigator.clipboard?.writeText(code);
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          }}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <div style={maxHeight ? { maxHeight, overflowY: "auto" } : undefined}>
        {html ? (
          <div dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <pre className="shiki mono overflow-x-auto px-3.5 py-3 text-[12.5px] leading-[1.6]">{code}</pre>
        )}
      </div>
    </div>
  );
}

const components = {
  pre: ({ children }: any) => <>{children}</>,
  code: ({ className, children, ...rest }: any) => {
    const m = /language-([\w+#-]+)/.exec(className || "");
    const text = String(children ?? "");
    const block = m || text.includes("\n");
    if (!block) return <code {...rest}>{children}</code>;
    return <CodeBlock code={text.replace(/\n$/, "")} lang={m?.[1]} />;
  },
  a: ({ href, children }: any) => (
    <a
      href={href}
      onClick={(e: React.MouseEvent) => {
        e.preventDefault();
        if (href) window.ee ? window.ee.openExternal(href) : window.open(href, "_blank");
      }}
    >
      {children}
    </a>
  ),
};

export const Markdown = memo(function Markdown({ text, streaming }: { text: string; streaming?: boolean }) {
  // 流式期间（T22）：纯文本渲染，跳过 ReactMarkdown 的全文同步解析。
  // 流式输出每个 delta 都变 text，长回复下每帧重解析会卡输入；终态（streaming=false）
  // 或老调用（不传 streaming）才一次性走 markdown。纯文本保留换行与字面量，不丢字。
  if (streaming) {
    return <div className="md text-[14px] text-fg stream-caret-last whitespace-pre-wrap break-words">{text}</div>;
  }
  return (
    <div className={"md text-[14px] text-fg"}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
