// MCP 设置页用的纯函数：命令行参数 / 环境变量 / 请求头 的文本 ⇄ 结构互转。

/** 按空白切分，支持 "双引号" 和 '单引号' 包住含空格的参数（不处理反斜杠转义，Windows 路径原样保留）。 */
export function parseArgs(text: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quote: string | null = null;
  let has = false;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
    } else if (/\s/.test(ch)) {
      if (has || cur) out.push(cur);
      cur = "";
      has = false;
    } else {
      cur += ch;
    }
  }
  if (has || cur) out.push(cur);
  return out;
}

export function formatArgs(args: string[]): string {
  return args.map((a) => (a === "" || /\s/.test(a) ? (a.includes('"') ? `'${a}'` : `"${a}"`) : a)).join(" ");
}

/** 每行一个 KEY=VALUE（sep="="）或 Key: Value（sep=":"）；空行和 # 注释忽略。 */
export function parseKv(text: string, sep: "=" | ":"): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const i = line.indexOf(sep);
    if (i <= 0) continue;
    const k = line.slice(0, i).trim();
    if (k) out[k] = line.slice(i + 1).trim();
  }
  return out;
}

export function formatKv(obj: Record<string, string> | undefined, sep: "=" | ":"): string {
  return Object.entries(obj ?? {})
    .map(([k, v]) => (sep === "=" ? `${k}=${v}` : `${k}: ${v}`))
    .join("\n");
}

export const MCP_NAME_RE = /^[A-Za-z0-9_-]{1,40}$/;

/** 把一行完整命令（如 `npx -y @scope/server C:\path`）拆成 command + args。 */
export function splitCommandLine(text: string): { command: string; args: string[] } {
  const parts = parseArgs(text.trim());
  return { command: parts[0] ?? "", args: parts.slice(1) };
}
