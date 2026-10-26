// 与后端 ee/server/threads.py 的条目结构一致。

export type ItemBase = { id: string; ts?: number; seq?: number };

export type AttachmentMeta = { id: string; name: string; mime: string; size: number; thumb?: string };
export type UserItem = ItemBase & { type: "user"; text: string; kind?: "review"; queued?: boolean; hist?: number; snap?: number; attachments?: AttachmentMeta[] };
export type AssistantItem = ItemBase & { type: "assistant"; text: string; reasoning: string; streaming?: boolean };
export type ToolStatus = "running" | "ok" | "error" | "denied" | "interrupted";
export type ToolItem = ItemBase & {
  type: "tool";
  call_id: string;
  name: string;
  args: Record<string, any>;
  summary: string;
  status: ToolStatus;
  output: string;
  diff: string;
  meta: Record<string, any>;
};
export type ApprovalStatus = "pending" | "once" | "always" | "deny" | "cancelled";
export type ApprovalItem = ItemBase & {
  type: "approval";
  call_id: string;
  tool: string;
  title: string;
  detail: string;
  prefix: string;
  status: ApprovalStatus;
  reason?: string;
};
export type PlanEntry = { content: string; status: "pending" | "in_progress" | "completed" | string };
export type PlanItem = ItemBase & { type: "plan"; items: PlanEntry[] };
export type NoticeItem = ItemBase & { type: "notice"; text: string; level: "info" | "warn" };
export type ErrorItem = ItemBase & { type: "error"; message: string };
export type TurnEndItem = ItemBase & { type: "turn_end"; reason: string; duration_ms: number; usage: Usage };

export type Item = UserItem | AssistantItem | ToolItem | ApprovalItem | PlanItem | NoticeItem | ErrorItem | TurnEndItem;

export type Usage = {
  requests?: number;
  prompt?: number;
  completion?: number;
  hit?: number;
  hit_rate?: number | null;
  cost?: number | null;
  currency?: string;
};

export type ThreadStatus = "idle" | "running" | "waiting_approval" | "compacting";

export type ThreadSummary = {
  id: string;
  title: string;
  cwd: string;
  model: string;
  mode: string;
  created?: number;
  updated?: number;
  status: ThreadStatus;
  parent_id?: string | null; // 分叉来源（D3-E）
};

export type ThreadDetail = ThreadSummary & {
  items: Item[];
  usage: Usage;
  context_left: number | null;
  shell: string | null;
  git: { is_repo: boolean; branch: string; changed: number; untracked: number } | null;
  instructions?: string[]; // 本会话加载的 AGENTS.md
  status_text?: string; // 临时状态：正在连接模型… 等（turn/status）
};

export type Workspace = { path: string; name: string; opened?: number; exists?: boolean };

export type ModelInfo = {
  name: string;
  provider: string;
  model: string;
  family: string;
  context_window: number;
  reasoning: boolean;
  vision?: boolean;
  has_key: boolean;
  price: { input: number | null; output: number | null; cache_read: number | null; currency: string };
};

export type ProviderSettings = {
  base_url: string;
  api_key_env: string;
  api_key?: string | null; // 只在保存时发送；读取时永远没有
  api_key_set?: boolean;
  api_key_preview?: string;
  env_present?: boolean;
  api_key_source?: KeySource;
  builtin?: boolean;
};

export type PriceSettings = {
  input: number | string | null;
  output: number | string | null;
  cache_read: number | string | null;
  currency: string | null;
};

export type ModelSettings = {
  provider: string;
  model: string;
  family: string;
  detected_family?: string;
  context_window: number | string | null;
  price: PriceSettings;
  default_price?: PriceSettings;
  default_context_window?: number;
  vision?: boolean | null; // null = 按模型档案自动
  default_vision?: boolean;
  builtin?: boolean;
};

export type Settings = {
  default_model: string;
  shell: string;
  max_steps: number;
  bash_timeout: number;
  permissions: { mode: string };
  ui: { theme: "system" | "dark" | "light"; notify?: "never" | "unfocused" | "always"; notify_approval?: boolean; close_to_tray?: boolean };
  network?: NetworkSettings;
  web?: WebSettings;
  subagent?: SubagentSettings;
  memory?: MemorySettings;
  mcp?: McpSettings;
  secrets?: SecretsInfo; // 只读：凭据存储后端说明
  providers: Record<string, ProviderSettings>;
  models: Record<string, ModelSettings>;
};

/** key 来源：store=凭据存储（Windows 凭据管理器），plain=旧版 config.toml 明文（启动时迁移），env=环境变量 */
export type KeySource = "store" | "plain" | "env" | "";
export type SecretsInfo = { backend: string; label: string; secure: boolean };

export function keySourceText(source: KeySource | undefined, preview: string | undefined, env: string | undefined): string {
  if (source === "store") return `已保存在凭据存储（${preview ?? ""}）`;
  if (source === "plain") return `config.toml 明文（${preview ?? ""}），下次启动会移进凭据存储`;
  if (source === "env") return `使用环境变量 ${env ?? ""}`;
  return "未设置";
}

export type WebKeySettings = {
  api_key?: string | null; // 只在保存时发送；"" = 清除
  api_key_env?: string;
  api_key_set?: boolean;
  api_key_preview?: string;
  env_present?: boolean;
  api_key_source?: KeySource;
};
export type WebSettings = {
  search_backend: "exa" | "duckduckgo" | "deepseek" | "tavily" | "brave" | "off" | string;
  fetch_backend: "auto" | "deepseek" | "direct" | "off" | string;
  search_fallback?: string[];
  timeout: number;
  max_results: number;
  deepseek_key_available?: boolean;
  keys: { tavily: WebKeySettings; brave: WebKeySettings };
};
/** 设置里的一个 MCP 服务器（[mcp.servers.<名>]）。stdio 用 command/args/env/cwd；远程用 url/headers/type。 */
export type McpServerSettings = {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
  type?: "" | "http" | "sse" | string;
  enabled: boolean;
  trusted: boolean;
};
export type McpSettings = {
  enabled: boolean;
  startup_timeout: number;
  tool_timeout: number;
  project_json: boolean;
  allowed_project: string[];
  servers: Record<string, McpServerSettings>;
};
export type McpToolInfo = { name: string; description: string; read_only: boolean };
/** mcp/list、mcp/test 返回的一行。 */
export type McpServerStatus = {
  name: string;
  transport: "stdio" | "http" | "sse" | string;
  command: string;
  args: string[];
  url: string;
  enabled: boolean;
  trusted: boolean;
  source: "user" | "project" | string;
  allow_id: string;
  status: "idle" | "connecting" | "ready" | "error" | "disabled" | "needs_approval" | string;
  error: string;
  tools: McpToolInfo[];
  server_info?: { name?: string; version?: string } | null;
};
export type McpListResponse = { enabled: boolean; servers: McpServerStatus[]; errors: string[]; project_json: string | null };
export type SubagentSettings = { enabled: boolean; max_steps: number };
export type MemorySettings = { enabled: boolean; workspace: boolean; max_bytes: number };
export type MemoryFile = { path: string; exists: boolean; text: string; entries: string[] };
export type MemoryInfo = { user: MemoryFile; workspace: MemoryFile | null };

/** 子代理进度记录：挂在 task 工具条目的 meta.subagent 上（后端 ee/core/subagent.py apply_event）。 */
export type SubagentRecord = {
  agent: string;
  description: string;
  model: string;
  mode: string;
  max_steps: number;
  status: "running" | "completed" | "max_steps" | "interrupted" | "error" | "loop" | string;
  step: number;
  tools: { id: string; name: string; summary: string; status: string }[];
  text: string;
  summary: string;
  notices?: string[];
  usage?: Usage & Record<string, any>;
};

export type NetworkSettings = {
  proxy_mode: "system" | "direct" | "custom";
  proxy_url: string;
  connect_timeout: number;
  read_timeout: number;
  retries: number;
  system_proxy?: string; // 只读：“跟随系统”时实际会用的代理说明
};

export type SettingsResponse = {
  path: string;
  project_sources: string[];
  settings: Settings;
  choices: { shells: string[]; modes: string[]; families: string[]; themes: string[]; search_backends?: string[]; fetch_backends?: string[] };
};

export type DiffFile = {
  path: string;
  old_path: string;
  status: "modified" | "added" | "deleted" | "renamed";
  additions: number;
  deletions: number;
  binary: boolean;
  patch: string;
};

export type GitReview = {
  is_repo: boolean;
  git_available: boolean;
  branch?: string;
  root?: string;
  files: DiffFile[];
  additions?: number;
  deletions?: number;
};

export type TermCreateResult =
  | { ok: true; id: number; pid: number; shell: string; kind: string; cwd: string }
  | { ok: false; error: string };

export interface TermBridge {
  available(): Promise<{ ok: boolean; error?: string }>;
  create(opts: { cwd?: string; shell?: { kind?: string | null; exe?: string | null }; cols?: number; rows?: number }): Promise<TermCreateResult>;
  write(id: number, data: string): void;
  resize(id: number, cols: number, rows: number): void;
  kill(id: number): Promise<boolean>;
  onData(cb: (m: { id: number; data: string }) => void): () => void;
  onExit(cb: (m: { id: number; code: number }) => void): () => void;
}

export type TreeEntry = { name: string; path: string; type: "dir" | "file"; size?: number | null; ignored?: boolean };

export type FileContent = {
  path: string;
  abs: string;
  name: string;
  size: number;
  kind: "text" | "image" | "binary";
  text?: string;
  encoding?: string;
  truncated?: boolean;
  lines?: number;
  mime?: string;
  data_url?: string;
  reason?: string;
};

export interface EeBridge {
  term?: TermBridge;
  platform: string;
  getBackend(): Promise<{ ok: boolean; port?: number; token?: string; error?: string }>;
  restartBackend(): Promise<{ ok: boolean; port?: number; token?: string; error?: string }>;
  openFolder(): Promise<string | null>;
  openPath(p: string): Promise<string>;
  showItem(p: string): Promise<void>;
  openExternal(url: string): Promise<void>;
  setTheme(t: string): Promise<boolean>;
  appInfo(): Promise<{ version: string; logs: string; packaged: boolean }>;
  onBackendExit(cb: (info: { code: number | null; tail: string }) => void): () => void;
  log?(line: string): void; // 写进 logs/backend.log
  // D4-5 桌面通知 + 托盘
  notify?(msg: { kind: string; threadId: string; title: string; body: string }): void;
  setPrefs?(p: { notify: "never" | "unfocused" | "always"; notifyApproval: boolean; closeToTray: boolean }): Promise<void>;
  setTrayStatus?(s: { running: number; waiting: number }): void;
  onOpenThread?(cb: (threadId: string) => void): () => void;
  onTrayAction?(cb: (action: string) => void): () => void;
  quitApp?(): void;
}

declare global {
  interface Window {
    ee?: EeBridge;
  }
}
