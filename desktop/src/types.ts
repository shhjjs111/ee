// 与后端 ee/server/threads.py 的条目结构一致。

export type ItemBase = { id: string; ts?: number; seq?: number };

export type AttachmentMeta = { id: string; name: string; mime: string; size: number; thumb?: string };
export type UserItem = ItemBase & { type: "user"; text: string; kind?: "review" | "loop"; queued?: boolean; queue?: "steer" | "follow"; hist?: number; snap?: number; attachments?: AttachmentMeta[] };
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
  /** 只给界面的结构化数据（后端 ee/tools/details.py），不进模型上下文。 */
  details: ToolDetails;
};
/** 工具结果 details：按 kind 有固定字段；所有 kind 都可能带 duration_ms / truncated / denied。 */
export type ToolTruncation = { lines: number; bytes: number; path: string | null };
export type FileChange = { verb: string; path: string; added: number; removed: number };
export type ToolDetails = {
  kind?: string;
  duration_ms?: number;
  truncated?: ToolTruncation;
  denied?: boolean;
  // edit
  files?: FileChange[] | string[];
  path?: string;
  verb?: string;
  added?: number;
  removed?: number;
  diff?: string;
  // shell
  command?: string;
  exit?: number | null;
  timed_out?: boolean;
  background?: boolean;
  bg_id?: string;
  // read / list / search
  start?: number;
  end?: number;
  total_lines?: number;
  total?: number;
  matches?: number;
  ref?: boolean;
  [k: string]: any;
};
export type ApprovalStatus = "pending" | "once" | "always" | "forever" | "deny" | "cancelled";
export type ApprovalItem = ItemBase & {
  type: "approval";
  call_id: string;
  tool: string;
  title: string;
  detail: string;
  prefix: string;
  status: ApprovalStatus;
  reason?: string;
  rule?: string; // 选「本项目以后都允许」时写入的永久规则（D4-8）；空 = 不能永久允许
  source?: ApprovalSource; // 子代理发出的审批：来自哪个子代理（D5-2）
};
/** 审批来源：并行子代理时界面据此标明是哪个子代理要的（D5-2）。 */
export type ApprovalSource = { call_id: string; agent: string; description: string };
// ask_user 提问（D4-8）
export type QuestionItem = ItemBase & {
  type: "question";
  call_id: string;
  question: string;
  options: string[];
  multiple: boolean;
  status: "pending" | "answered" | "cancelled";
  answer?: string;
};
export type PlanEntry = { content: string; status: "pending" | "in_progress" | "completed" | string };
export type PlanItem = ItemBase & { type: "plan"; items: PlanEntry[] };
export type NoticeItem = ItemBase & { type: "notice"; text: string; level: "info" | "warn" };
export type ErrorItem = ItemBase & { type: "error"; message: string };
export type TurnEndItem = ItemBase & { type: "turn_end"; reason: string; duration_ms: number; usage: Usage };

export type Item = UserItem | AssistantItem | ToolItem | ApprovalItem | QuestionItem | PlanItem | NoticeItem | ErrorItem | TurnEndItem;

export type Usage = {
  requests?: number;
  prompt?: number;
  completion?: number;
  hit?: number;
  hit_rate?: number | null;
  cost?: number | null;
  currency?: string;
  reasoning?: number; // 推理 token（服务端给了才有，D5-9）
  pruned?: number; // 修剪旧工具输出累计省下的 token（估计，D5-10）
  by_model?: Record<string, { requests: number; prompt: number; completion: number; reasoning: number; cost: number | null; currency: string }>;
};

/** 推理强度档位（D5-9）：""=默认（不发参数）| low | medium | high | max（最高）| off（关闭思考） */
export type Effort = "" | "low" | "medium" | "high" | "max" | "off";
/** levels：这个模型可选的强度档（不含 默认 / 关闭思考；DeepSeek 只有 low、max）；labels：档位文字覆盖（"" = 默认档） */
export type EffortSupport = { effort: boolean; off: boolean; levels?: string[]; labels?: Record<string, string> };

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
  /** 指挥关系（D6-1）：只有被指挥会话派发的子会话才有值；普通分叉不写它。 */
  orchestrator_parent_id?: string | null;
  orchestrator_role?: "orchestrator" | "child" | null;
  worktree?: { path: string; branch: string; repo: string; name: string } | null; // D5-1
  workspace?: string; // 归属的项目目录（worktree 对话归到主仓库）
  effort?: Effort | string; // D5-9：会话级推理强度
  effort_support?: EffortSupport | null;
  style?: string; // D5-9：会话级回答风格
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
  effort_support?: EffortSupport;
  effort_values?: Record<string, string>;
};

/** styles/list 的一项（D5-9） */
export type StyleInfo = { id: string; label: string; description: string; source: string; path: string };
export type BudgetSettings = { session: number | string; daily: number | string; monthly: number | string; action: "warn" | "pause" | string; warn_ratio: number | string; currency: string };
export type BudgetStatus = { currency: string; action: string; warn_ratio: number; scopes: Record<"session" | "daily" | "monthly", { spent: number; limit: number }> };

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
  /** D5-8 命令沙箱：supported 只读（仅 Windows 生效） */
  sandbox?: SandboxSettings;
  memory?: MemorySettings;
  skills?: SkillsSettings;
  /** 精简模式：full 完整（默认）/ lean 只有 read/write/edit/shell + 短提示词；新对话生效 */
  profile?: "full" | "lean";
  todo?: boolean;
  present_plan?: boolean;
  mcp?: McpSettings;
  secrets?: SecretsInfo; // 只读：凭据存储后端说明
  providers: Record<string, ProviderSettings>;
  models: Record<string, ModelSettings>;
  reasoning?: { effort: Effort | string };
  roles?: Record<string, string>;
  title?: { ai: boolean };
  style?: { default: string };
  budget?: BudgetSettings;
  queue?: { steering_mode: "one" | "all"; follow_up_mode: "one" | "all" }; // 运行中排队消息的送入方式
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
  /** 用户侧记过信任的项目服务器（name:sha8）。项目 .mcp.json 自己写的 trusted 不生效。 */
  trusted_project?: string[];
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
export type SandboxSettings = { enabled: boolean; network: boolean; supported?: boolean };
export type SubagentSettings = { enabled: boolean; max_steps: number; max_parallel?: number; worktree?: boolean };
export type MemorySettings = { enabled: boolean; workspace: boolean; max_bytes: number; inject?: boolean; tool?: boolean };
/** [skills]：总开关、兼容 .claude/.agents 目录、禁用的技能名、清单长度上限（D5-5）。 */
export type SkillsSettings = { enabled: boolean; compat: boolean; disabled: string[]; max_chars: number };
export type MemoryFile = { path: string; exists: boolean; text: string; entries: string[] };
export type MemoryInfo = { user: MemoryFile; workspace: MemoryFile | null };

/** 子代理进度记录：挂在 task 工具条目的 meta.subagent 上（后端 ee/core/subagent.py apply_event）。 */
export type SubagentWorktree = {
  path: string;
  branch: string;
  base?: string;
  synced_dirty?: boolean;
  files?: { path: string; additions: number; deletions: number; binary?: boolean }[];
  file_count?: number;
  additions?: number;
  deletions?: number;
  commits?: string[];
  removed?: boolean;
  kept?: boolean;
  kept_reason?: string; // 没删的原因（diff 失败/结果未知），界面要说事实
};
export type SubagentRecord = {
  id?: string;
  agent: string;
  description: string;
  model: string;
  mode: string;
  max_steps: number;
  status: "queued" | "running" | "completed" | "max_steps" | "interrupted" | "error" | "loop" | string;
  step: number;
  tools: { id: string; name: string; summary: string; status: string }[];
  text: string;
  summary: string;
  notices?: string[];
  usage?: Usage & Record<string, any>;
  worktree?: SubagentWorktree; // 写型子代理在独立 git worktree 里并行时（D5-2）
  fork?: boolean; // 分叉子代理：继承主会话上下文（D5-10）
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
  // 项目 .ee/config.toml 里因安全原因被忽略的字段（项目配置只能收紧、不能放宽）
  project_ignored?: { path: string; fields: string[] }[];
  settings: Settings;
  choices: {
    shells: string[];
    modes: string[];
    families: string[];
    themes: string[];
    search_backends?: string[];
    fetch_backends?: string[];
    efforts?: string[];
    roles?: string[];
    budget_actions?: string[];
    styles?: StyleInfo[];
  };
};

export type HunkInfo = { id: string; header: string; additions: number; deletions: number; accepted: boolean };

export type DiffFile = {
  path: string;
  old_path: string;
  status: "modified" | "added" | "deleted" | "renamed";
  additions: number;
  deletions: number;
  binary: boolean;
  too_large?: boolean;
  patch: string;
  hunks?: HunkInfo[];
};

export type GitReview = {
  is_repo: boolean;
  git_available: boolean;
  branch?: string;
  root?: string;
  files: DiffFile[];
  additions?: number;
  deletions?: number;
  /** 非 git 项目：用会话的磁盘快照（会话开始 → 当前）对比（D5-7） */
  snapshot?: { available: boolean; reason?: string; empty?: boolean; base?: string; since?: number | null };
};

/** 检查点时间线（D5-7）：kind=turn 是一个回合（回合前/后），kind=restore 是恢复前自动存的状态 */
export type Checkpoint = {
  id: string;
  kind: "turn" | "restore";
  turn?: number;
  ts: number | null;
  text: string;
  before: string | null;
  after: string | null;
  undone: boolean;
  running: boolean;
  expired: boolean;
  after_expired: boolean;
  stat: { files: number; additions: number; deletions: number } | null;
};

export type CheckpointList = { available: boolean; reason?: string; checkpoints: Checkpoint[] };

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

// 后台命令（D4-6，后端 ee/core/bgproc.py BgProc.info()）
export type BgProc = {
  id: string;
  command: string;
  cwd: string;
  status: "running" | "exited" | "killed" | "failed" | string;
  exit_code: number | null;
  pid: number | null;
  started: number;
  ended: number | null;
  duration: number;
  output_chars: number;
  unread: number;
  log_path: string;
};

// git worktree（D5-1，后端 ee/core/worktree.py）
export type WorktreeInfo = {
  name: string;
  path: string;
  repo: string;
  branch: string;
  base: string;
  base_ref?: string;
  created: number;
  included?: string[];
  exists?: boolean;
  changed?: number;
  untracked?: number;
  commits?: number;
  in_use?: string; // 非空：自动化正在里面运行（D5-3），不能合并/删除
};

/** worktree/diff：ok=false 时 files 为空不代表没有改动（后端 git 读取失败）。 */
export type WorktreeDiff = {
  worktree: WorktreeInfo;
  files: { path: string; additions: number; deletions: number; status?: string; binary?: boolean }[];
  additions: number;
  deletions: number;
  ok: boolean;
  error: string;
};

/** worktree/remove：backup_skipped 非空表示有被忽略的文件没能备份（默认拒绝删除）。 */
export type WorktreeRemoveResult = {
  removed: boolean;
  autosaved?: string | null;
  branch: string;
  branch_deleted: boolean;
  ignored_backup: string | null;
  backed_up: string[];
  backup_skipped: string[];
  discarded_unbacked?: string[];
};

/** thread/context_breakdown（后端 ee/core/breakdown.py）：上下文构成。token 为估算。 */
export type ContextBreakdown = {
  items: { key: string; label: string; tokens: number; percent: number }[];
  tools: { name: string; tokens: number }[];
  total: number;
  window: number;
  used: number;
  actual: boolean;
  images: number;
  messages: number;
  savings: { pruned: number; prunes: number; pruned_outputs: number; compacted: number };
  compactions: number;
  frozen: boolean;
  thresholds: { compact: number | null; prune: number | null };
  model: string;
  estimated: boolean;
};
