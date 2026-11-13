// 与后端 ee/server/threads.py 的条目结构一致。

export type ItemBase = { id: string; ts?: number; seq?: number };

export type AttachmentMeta = { id: string; name: string; mime: string; size: number; thumb?: string };
export type UserItem = ItemBase & { type: "user"; text: string; kind?: "review" | "loop"; queued?: boolean; queue?: "steer" | "follow"; hist?: number; snap?: number; attachments?: AttachmentMeta[]; goal?: { command: string; max_rounds: number; timeout?: number; pass_to_pass?: string }; allout?: { enabled: boolean; candidates: number } };

/** 全力以赴的火力推导结果（后端 core/allout.py 的 AlloutPlan.as_dict）。 */
export type AlloutPlanInfo = {
  candidates: number;
  repair_rounds: number;
  max_rounds: number;
  effort: string;
  max_steps: number;
  temperature_ladder: number[];
  verified: boolean;
  reasons: string[];
  degradations: string[];
};
export type AssistantItem = ItemBase & { type: "assistant"; text: string; reasoning: string; streaming?: boolean };
export type ToolStatus = "running" | "ok" | "error" | "denied" | "interrupted";
export type ToolItem = ItemBase & {
  type: "tool";
  call_id: string;
  name: string;
  args: Record<string, unknown>;   // 模型产出的工具参数：形状由各工具决定，读的地方统一 String(...) 收敛
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
  // 自动审批的建议态（T10-c）：只描述「如果将来放行，会放行什么」。
  // 它不是决策——界面只展示，不提供放行入口。缺省表示没有建议（老数据）。
  suggestion?: ApprovalSuggestion;
};
/** 自动审批建议（后端 core/autoapprove.py 的 Suggestion.as_dict）。 */
export type ApprovalSuggestion = {
  eligible: boolean;
  rule: string;
  blocked_by: string;
  summary: string;
  hits: number;
  reason: string;
  action: string;
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
  hit_coverage?: number | null;
  /** 主对话口径（P0-1）：不含标题 / commit / 压缩 / 子代理这些命中恒 0 的侧路请求。
   *  旧后端没有这些字段，界面读不到时回落到 hit_rate。 */
  main_hit_rate?: number | null;
  main_hit_coverage?: number | null;
  main_requests?: number;
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

/** 一次派发（D6-1 U2）。`terminal_reason` 只有跑完才有值；`status=running` 且进程已死时
 *  后端会在读取时归一成 `done + interrupted`。 */
export type ThreadRun = {
  id: string;
  started: number;
  finished?: number | null;
  status: "running" | "done";
  terminal_reason?: string | null;
  initiator?: string;
};

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
  /** 派发记录（D6-1 U2）：末条带 terminal_reason。判「跑完了」还是「被杀/超预算」只能看它，
   *  status 的 idle 同时覆盖两种情况。 */
  runs?: ThreadRun[];
  /** 末条 run 的终态（D6-1 U8）：`thread/status` 直接给这个平铺字段，卡片按它判成败。 */
  terminal_reason?: string | null;
  /** 子会话 worktree 的改动数（D6-1 U8）：由 thread_status 给出，列表里不查也能显示。 */
  changed_count?: number;
  worktree?: { path: string; branch: string; repo: string; name: string } | null; // D5-1
  workspace?: string; // 归属的项目目录（worktree 对话归到主仓库）
  effort?: Effort | string; // D5-9：会话级推理强度
  effort_support?: EffortSupport | null;
  style?: string; // D5-9：会话级回答风格
  /** 会话记录写盘失败（磁盘满 / 文件被占用）：{count, error, at}。
   *  后端只能放在内存里（盘写不进去），所以这是「现在保存不了」的实时状态，不是落盘字段。 */
  save_error?: SaveError | null;
};

/** 会话记录写入失败的状态（Thread._on_store_fail）。count 是累计失败次数。 */
export type SaveError = { count: number; error: string; at: number };

export type ThreadDetail = ThreadSummary & {
  items: Item[];
  usage: Usage;
  context_left: number | null;
  shell: string | null;
  git: { is_repo: boolean; branch: string; changed: number; untracked: number } | null;
  instructions?: string[]; // 本会话加载的 AGENTS.md
  status_text?: string; // 临时状态：正在连接模型… 等（turn/status）
  /** 分页信息（首屏只拉尾部一页时用）：本会话总条目数 */
  items_total?: number;
  /** 上面返回的 items 前面还有更早的历史（需要再拉） */
  items_has_more?: boolean;
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
  /** 一个回合最多续跑几段（1 = 到 max_steps 就停）；总步数上限 = max_steps × 段数 */
  max_steps_segments?: number;
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
  /** 内存里的会话缓存（用户级配置，项目层不能改）：超过上限就淘汰最久没用的空闲会话。 */
  session?: { max_loaded_threads?: number; evict_idle_seconds?: number };
  /** 精简模式：full 完整（默认）/ lean 只有 read/write/edit/shell + 短提示词；新对话生效 */
  profile?: "full" | "lean";
  todo?: boolean;
  present_plan?: boolean;
  /** LSP 诊断（D6-2）：开启后模型能看到语言服务器的类型错误。
   *  注意：开启时 repo_map 工具会让位（见 tools/__init__.py），关掉就回来。 */
  lsp?: { enabled?: boolean };
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
  usage?: Usage;   // 后端传的就是 Usage（多给的字段没人读，不必再 & Record<string, unknown>）
  worktree?: SubagentWorktree; // 写型子代理在独立 git worktree 里并行时（D5-2）
  fork?: boolean; // 分叉子代理：继承主会话上下文（D5-10）
};

/** 一个在跑的语言服务器（`lsp/servers`，按会话）。服务器不常驻，这是快照。 */
export type LspServerInfo = {
  thread_id: string;
  title: string;
  root: string;
  language: string;
  identity: string;
  refs: number;
  alive: boolean;
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
  // 自动更新（electron/updater.ts）。可选：老版本 bridge 或浏览器调试时没有。
  update?: {
    state(): Promise<UpdateState>;
    check(): Promise<UpdateState>;
    install(): Promise<boolean>;
    openRelease(): Promise<void>;
    onState(cb: (s: UpdateState) => void): () => void;
  };
  captureWindow?(): Promise<{ data_url?: string; error?: string }>;
  /** 拖入/粘贴/选中的文件的真实路径。Electron 32 起 `File.path` 被移除，只能走
   *  主进程的 `webUtils.getPathForFile`（electron/preload.ts）。返回空串表示这个
   *  File 不是磁盘上的文件（浏览器调试模式、或 JS 里 new 出来的），调用方要兜住。 */
  getFilePath?(f: File): string;
  /** 把图片另存为到用户选的位置（主进程弹保存对话框 + 写盘）。
   *  canceled 表示用户点了取消；error 是写盘失败的原因。 */
  saveImage?(name: string, dataUrl: string): Promise<{ ok: boolean; path?: string; canceled?: boolean; error?: string }>;
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

/** 自动更新状态（与 desktop/electron/updater.ts 的 UpdateState 一致）。
 *  改动字段时两边一起改：主进程负责发，这里是渲染进程的接收端。 */
export type UpdateState = {
  phase: "idle" | "checking" | "available" | "downloading" | "downloaded" | "up-to-date" | "error";
  version?: string;
  percent?: number;
  message?: string;
  current: string;
};

// 后台命令（D4-6，后端 ee/core/bgproc.py BgProc.info()）
/** 子会话阻塞在审批或提问上（D6-1 U8，通知 `thread/child_approval`）。
 *  用户的注意力在父会话，这条把「哪个子任务卡住了、卡的什么」带到父界面。 */
export type ChildApprovalNote = {
  parent_id: string;
  thread_id: string;
  kind: "approval" | "question" | string;
  child_title: string;
  call_id: string;
  tool: string;
  summary: string;
  detail?: string;
};

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
  savings: { pruned: number; prunes: number; pruned_outputs: number; compacted: number; reasoning?: number; reasoning_cut?: number };
  /** 缓存命中（T19）：hit_rate/hit_coverage 未知时是 null（不是 0）。
   *  main_* 是主对话口径（P0-1），不含标题 / commit / 压缩 / 子代理这些命中恒 0 的侧路请求；
   *  旧后端没有这几个字段，界面读不到时回落到 hit_*。 */
  cache: {
    hit: number;
    hit_rate: number | null;
    hit_coverage: number | null;
    main_hit_rate?: number | null;
    main_hit_coverage?: number | null;
    main_requests?: number;
    /** 请求口径的覆盖率（报过 hit 的请求 / 全部主对话请求）。token 口径看不见缺用量
     *  的请求（它们 prompt=0），所以这个值掉下 100% 才说明样本真的不全。 */
    hit_coverage_requests?: number | null;
    /** 主对话里没有返回用量的请求数（命中率分母里根本没有它们）。 */
    usage_missing?: number;
    /** 冷启动/无基线请求数。 */
    cold?: number;
    /** 空闲超过 provider 缓存 TTL 的请求数（服务端过期，不是前缀不稳）。 */
    cold_ttl?: number;
    /** 老账本里分不清冷启动与整段未命中的行数（不算成冷启动，也不进正常回合）。 */
    legacy?: number;
    /** 已知断点原因计数：compact / prune / mcp_attach / mcp_change / model_switch / prefix_migrated... */
    causes?: Record<string, number>;
    /** 客户端前缀稳定率（相邻请求的 wire 指纹只是追加）。旧后端/旧账本没有指纹时缺失。 */
    prefix_rate?: number | null;
    /** 参与稳定率计算的请求数（有上一次请求可比的次数）。 */
    prefix_observed?: number;
    /** 扣掉末尾不满一个缓存块的 token 后的命中率（块大小由 provider 档案决定）。 */
    tail_rate?: number | null;
    /** 累计的尾块 token（每次请求末尾不满一块的部分）。 */
    tail_tokens?: number;
  };
  compactions: number;
  frozen: boolean;
  thresholds: { compact: number | null; prune: number | null; prune_tokens?: number | null; compact_tokens?: number | null };
  model: string;
  estimated: boolean;
};

/** 文件交接清单条目（T10-e，后端 core/handoff.py 的 Entry.as_dict）。 */
export type HandoffEntry = {
  path: string;
  category: "staged" | "unstaged" | "untracked" | "ignored" | "tracked" | string;
  kind: "file" | "dir" | "symlink" | string;
  size: number;
  sha256: string;
  binary: boolean;
  selected: boolean;
};

/** 迁移清单（RPC handoff/manifest）。 */
export type HandoffManifest = {
  root: string;
  ignored_policy: "skip" | "include" | string;
  entries: HandoffEntry[];
  summary: {
    total: number;
    selected: number;
    bytes: number;
    binary: number;
    by_category: Record<string, number>;
  };
};

/** best-of-n 的候选（RPC bestof/pick）。 */
export type BestofCandidate = {
  id: string;
  title: string;
  worktree: string;
  run_id: string;
  cost: number;
  verified: boolean;
  met: boolean;
  self_reported_ok: boolean;
  /** 验证后代码变过（T10-d / ACC-16）：证据过期，不能再声称已验证。 */
  stale?: boolean;
  /** 跑判据时的代码状态指纹；带回来给 bestof/recheck 比对。 */
  verified_state?: string | null;
  current_state?: string | null;
  reason: string;
  /** allout 的质量信号（老后端不返回时当作中性默认）。 */
  regressed?: boolean;
  votes?: number;
  touched_tests?: boolean;
  files?: number;
  additions?: number;
  deletions?: number;
};
