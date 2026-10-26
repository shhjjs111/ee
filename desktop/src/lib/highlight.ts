// Shiki 代码高亮：按需加载语言，JS 正则引擎（不需要 wasm），light/dark 双主题用 CSS 变量切换。
import { createHighlighterCore, type HighlighterCore } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";

const LANGS: Record<string, () => Promise<any>> = {
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  javascript: () => import("shiki/langs/javascript.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  python: () => import("shiki/langs/python.mjs"),
  shellscript: () => import("shiki/langs/shellscript.mjs"),
  powershell: () => import("shiki/langs/powershell.mjs"),
  bat: () => import("shiki/langs/bat.mjs"),
  diff: () => import("shiki/langs/diff.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  html: () => import("shiki/langs/html.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  java: () => import("shiki/langs/java.mjs"),
  c: () => import("shiki/langs/c.mjs"),
  cpp: () => import("shiki/langs/cpp.mjs"),
  csharp: () => import("shiki/langs/csharp.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
};

const ALIAS: Record<string, string> = {
  ts: "typescript", js: "javascript", mjs: "javascript", cjs: "javascript", py: "python", sh: "shellscript",
  bash: "shellscript", zsh: "shellscript", shell: "shellscript", console: "shellscript", ps1: "powershell",
  pwsh: "powershell", ps: "powershell", cmd: "bat", batch: "bat", yml: "yaml", md: "markdown", rs: "rust",
  "c++": "cpp", cc: "cpp", h: "c", hpp: "cpp", cs: "csharp", "c#": "csharp", patch: "diff", htm: "html",
};

let hl: Promise<HighlighterCore> | null = null;
const loaded = new Set<string>();

function highlighter(): Promise<HighlighterCore> {
  if (!hl) {
    hl = createHighlighterCore({
      themes: [import("shiki/themes/github-light-default.mjs"), import("shiki/themes/github-dark-default.mjs")],
      langs: [],
      engine: createJavaScriptRegexEngine(),
    });
  }
  return hl;
}

export function normalizeLang(lang?: string): string | null {
  if (!lang) return null;
  const l = lang.toLowerCase();
  const n = ALIAS[l] ?? l;
  return LANGS[n] ? n : null;
}

export function langFromPath(path: string): string | null {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return normalizeLang(ext);
}

const cache = new Map<string, string>();

export async function highlight(code: string, lang?: string | null): Promise<string | null> {
  const l = normalizeLang(lang ?? undefined);
  if (!l) return null;
  const key = `${l}\u0000${code}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const h = await highlighter();
  if (!loaded.has(l)) {
    await h.loadLanguage((await LANGS[l]()).default);
    loaded.add(l);
  }
  const html = h.codeToHtml(code, {
    lang: l,
    themes: { light: "github-light-default", dark: "github-dark-default" },
    defaultColor: false,
  });
  if (cache.size > 300) cache.clear();
  cache.set(key, html);
  return html;
}
