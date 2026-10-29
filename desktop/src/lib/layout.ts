// 布局计算的纯函数（本文件不碰 DOM、不读 localStorage）：侧栏、工具面板与中区宽度都在这里定。
//
// 为什么单独抽出来：改版前右栏宽度只在「拖动」事件里算上限（RightPanel.tsx 的 onMove），
// 窗口缩小、首次打开、双击复位三条路径各自处理，容易出现「中区被压到 296px」的情况。
// 现在所有路径都过 computeLayout，窗口一变就重算，规则只有一份。

export const SIDEBAR_MIN = 216;
export const SIDEBAR_MAX = 280;
export const SIDEBAR_DEFAULT = 248;

export const INSPECTOR_MIN = 360;
export const INSPECTOR_MAX = 560;
export const INSPECTOR_DEFAULT = 420;

/** 中区（对话区）必须保留的最小可用宽度。低于它就改用覆盖式工具面板。 */
export const CONTENT_MIN = 560;

/** 侧栏与中区、中区与工具面板之间的分隔线宽度。 */
const DIVIDER = 1;

export type LayoutPrefs = {
  version: 1;
  /** 侧栏宽度（展开时生效）。 */
  sidebar: number;
  sidebarCollapsed: boolean;
  /** 工具面板首选宽度；窗口够宽时按它停靠。 */
  inspector: number;
  inspectorOpen: boolean;
};

/** 工具面板的展现方式：dock＝占位停靠，overlay＝覆盖在中区之上。 */
export type InspectorMode = "dock" | "overlay";

export type LayoutResult = {
  sidebar: number;
  inspector: number;
  inspectorMode: InspectorMode;
  /** 中区可用宽度（已扣掉实际占位的侧栏、分隔线与停靠式工具面板）。 */
  contentWidth: number;
};

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

function finiteNumber(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

export function defaultPrefs(): LayoutPrefs {
  return {
    version: 1,
    sidebar: SIDEBAR_DEFAULT,
    sidebarCollapsed: false,
    inspector: INSPECTOR_DEFAULT,
    // 首次使用默认收起工具面板：Codex 式观感的关键是「对话优先」，不是一屏三栏。
    inspectorOpen: false,
  };
}

export function computeLayout(input: { windowWidth: number; prefs: LayoutPrefs }): LayoutResult {
  const { prefs } = input;
  // 窗口宽度可能是 0（首帧、最小化）：按默认值兜底，避免算出负宽度。
  const windowWidth = clamp(
    Math.round(finiteNumber(input.windowWidth) ?? 0) || 1360,
    320,
    Number.MAX_SAFE_INTEGER,
  );

  const sidebar = prefs.sidebarCollapsed ? 0 : clamp(Math.round(prefs.sidebar), SIDEBAR_MIN, SIDEBAR_MAX);
  const sidebarDivider = sidebar > 0 ? DIVIDER : 0;
  const rest = windowWidth - sidebar - sidebarDivider;

  if (!prefs.inspectorOpen) {
    return { sidebar, inspector: 0, inspectorMode: "dock", contentWidth: rest };
  }

  const wanted = clamp(Math.round(prefs.inspector), INSPECTOR_MIN, INSPECTOR_MAX);

  // 停靠必须给中区留够 CONTENT_MIN；留不够就改成覆盖，绝不再压缩对话。
  if (rest - DIVIDER - wanted >= CONTENT_MIN) {
    return {
      sidebar,
      inspector: wanted,
      inspectorMode: "dock",
      contentWidth: rest - DIVIDER - wanted,
    };
  }

  // 覆盖式：宽度受窗口限制，但不会窄过 INSPECTOR_MIN（再窄就不如让用户关掉）。
  const room = Math.max(0, rest - CONTENT_MIN / 4);
  const inspector = Math.max(INSPECTOR_MIN, Math.min(wanted, room));
  return { sidebar, inspector, inspectorMode: "overlay", contentWidth: rest };
}

/** 校验一份 v1 偏好；任何字段不合法就整体判为不可用，回退默认。 */
function validPrefs(raw: unknown): LayoutPrefs | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  if (o.version !== 1) return null;
  const sidebar = finiteNumber(o.sidebar);
  const inspector = finiteNumber(o.inspector);
  if (sidebar === null || inspector === null) return null;
  if (typeof o.sidebarCollapsed !== "boolean" || typeof o.inspectorOpen !== "boolean") return null;
  return {
    version: 1,
    sidebar: clamp(Math.round(sidebar), SIDEBAR_MIN, SIDEBAR_MAX),
    sidebarCollapsed: o.sidebarCollapsed,
    inspector: clamp(Math.round(inspector), INSPECTOR_MIN, INSPECTOR_MAX),
    inspectorOpen: o.inspectorOpen,
  };
}

/**
 * 从旧的 localStorage 键迁移到 v1 偏好。
 *
 * 旧键（改版前就在用的）：`ee.right`（"1"/"0"）、`ee.rightWidth`（320–820 的字符串）。
 * 迁移只搬"展示偏好"，不碰任何会话数据、密钥或 token。
 * 新键 `ee.layout` 已存在且合法时原样沿用，不重复迁移。
 */
export function migratePrefs(old: {
  layout?: string | null;
  right?: string | null;
  rightWidth?: string | null;
}): LayoutPrefs {
  if (old.layout) {
    try {
      const parsed = validPrefs(JSON.parse(old.layout));
      if (parsed) return parsed;
    } catch {
      /* 坏 JSON 走下面的旧键迁移，最终回退默认 */
    }
  }

  const out = defaultPrefs();

  // 旧：只有明确的 "1" 才算展开；"0"、空、乱值都是收起。
  if (old.right === "1") out.inspectorOpen = true;

  const w = finiteNumber(old.rightWidth);
  if (w !== null && w > 0) {
    // 旧范围 320–820，新范围 360–560：夹紧而不是丢弃用户的选择。
    out.inspector = clamp(Math.round(w), INSPECTOR_MIN, INSPECTOR_MAX);
  }

  return out;
}
