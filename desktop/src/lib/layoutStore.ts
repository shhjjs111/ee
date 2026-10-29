// 布局偏好（版本化，独立于业务 store）：侧栏宽度/收起、工具面板宽度/开关。
//
// 只存"展示偏好"，不存会话数据、token、密钥。旧键（ee.right / ee.rightWidth）由
// migratePrefs 一次性迁移，迁移后仍继续写旧键做兼容，避免回退到旧版本时偏好丢失。

import { create } from "zustand";
import { computeLayout, defaultPrefs, migratePrefs, type LayoutPrefs, type LayoutResult } from "./layout";

const KEY = "ee.layout";
const LEGACY_RIGHT = "ee.right";
const LEGACY_WIDTH = "ee.rightWidth";

function readPrefs(): LayoutPrefs {
  try {
    return migratePrefs({
      layout: localStorage.getItem(KEY),
      right: localStorage.getItem(LEGACY_RIGHT),
      rightWidth: localStorage.getItem(LEGACY_WIDTH),
    });
  } catch {
    return defaultPrefs(); // localStorage 不可用（隐私模式等）时按默认走
  }
}

function writePrefs(p: LayoutPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(p));
    // 兼容旧版本：仍是同一份偏好，只是换了个键名
    localStorage.setItem(LEGACY_RIGHT, p.inspectorOpen ? "1" : "0");
    localStorage.setItem(LEGACY_WIDTH, String(p.inspector));
  } catch {
    /* 写不进去不影响当前会话内的使用 */
  }
}

type LayoutState = {
  prefs: LayoutPrefs;
  /** 窗口宽度；由 AppShell 监听 resize 写入，纯函数据此算布局。 */
  windowWidth: number;
  layout: LayoutResult;

  setWindowWidth(w: number): void;
  set(p: Partial<LayoutPrefs>): void;
  toggleSidebar(): void;
  setInspectorOpen(open: boolean): void;
  /** 拖动结束时提交宽度（拖动过程中不要每帧写盘）。 */
  setInspectorWidth(w: number): void;
};

function derive(prefs: LayoutPrefs, windowWidth: number): LayoutResult {
  return computeLayout({ windowWidth, prefs });
}

const initialPrefs = typeof localStorage === "undefined" ? defaultPrefs() : readPrefs();
const initialWidth = typeof window === "undefined" ? 1360 : window.innerWidth;

export const useLayout = create<LayoutState>((set, get) => ({
  prefs: initialPrefs,
  windowWidth: initialWidth,
  layout: derive(initialPrefs, initialWidth),

  setWindowWidth(w) {
    if (!Number.isFinite(w) || w <= 0) return;
    const cur = get();
    if (Math.abs(cur.windowWidth - w) < 1) return; // 避免同值重排
    set({ windowWidth: w, layout: derive(cur.prefs, w) });
  },

  set(p) {
    const prefs = { ...get().prefs, ...p, version: 1 as const };
    writePrefs(prefs);
    set({ prefs, layout: derive(prefs, get().windowWidth) });
  },

  toggleSidebar() {
    get().set({ sidebarCollapsed: !get().prefs.sidebarCollapsed });
  },

  setInspectorOpen(open) {
    get().set({ inspectorOpen: open });
  },

  setInspectorWidth(w) {
    if (!Number.isFinite(w)) return;
    get().set({ inspector: Math.round(w) });
  },
}));

/** 测试用：把 store 重置到干净的默认值。 */
export function resetLayoutForTest(): void {
  const prefs = defaultPrefs();
  useLayout.setState({ prefs, windowWidth: 1360, layout: derive(prefs, 1360) });
}
