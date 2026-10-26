// 与后端 ee/server/threads.py 的条目结构一致。

export type ItemBase = { id: string; ts?: number; seq?: number };

export type UserItem = ItemBase & { type: "user"; text: string; queued?: boolean };
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

export type ThreadStatus = "idle" | "running" | "waiting_approval";

export type ThreadSummary = {
  id: string;
  title: string;
  cwd: string;
  model: string;
  mode: string;
  created?: number;
  updated?: number;
  status: ThreadStatus;
};

export type ThreadDetail = ThreadSummary & {
  items: Item[];
  usage: Usage;
  context_left: number | null;
  shell: string | null;
  git: { is_repo: boolean; branch: string; changed: number; untracked: number } | null;
};

export type Workspace = { path: string; name: string; opened?: number; exists?: boolean };

export type ModelInfo = {
  name: string;
  provider: string;
  model: string;
  family: string;
  context_window: number;
  reasoning: boolean;
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
  builtin?: boolean;
};

export type Settings = {
  default_model: string;
  shell: string;
  max_steps: number;
  bash_timeout: number;
  permissions: { mode: string };
  ui: { theme: "system" | "dark" | "light" };
  providers: Record<string, ProviderSettings>;
  models: Record<string, ModelSettings>;
};

export type SettingsResponse = {
  path: string;
  project_sources: string[];
  settings: Settings;
  choices: { shells: string[]; modes: string[]; families: string[]; themes: string[] };
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

export interface EeBridge {
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
}

declare global {
  interface Window {
    ee?: EeBridge;
  }
}
