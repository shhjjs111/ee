import { describe, expect, it } from "vitest";
import {
  CONTENT_MIN,
  INSPECTOR_DEFAULT,
  INSPECTOR_MAX,
  INSPECTOR_MIN,
  SIDEBAR_DEFAULT,
  SIDEBAR_MAX,
  SIDEBAR_MIN,
  computeLayout,
  defaultPrefs,
  migratePrefs,
} from "./layout";

describe("layout：侧栏与工具面板宽度", () => {
  it("宽窗口下工具面板停靠，且中区不小于保护宽度", () => {
    const r = computeLayout({ windowWidth: 1360, prefs: { ...defaultPrefs(), inspectorOpen: true } });
    expect(r.sidebar).toBe(SIDEBAR_DEFAULT);
    expect(r.inspectorMode).toBe("dock");
    expect(r.inspector).toBe(INSPECTOR_DEFAULT);
    expect(r.contentWidth).toBeGreaterThanOrEqual(CONTENT_MIN);
  });

  it("窗口缩到最小宽度时工具面板改为覆盖，不继续压缩对话", () => {
    const r = computeLayout({ windowWidth: 960, prefs: { ...defaultPrefs(), inspectorOpen: true } });
    expect(r.inspectorMode).toBe("overlay");
    // 覆盖式不占内容宽度：对话可用宽度只减去侧栏与分隔线
    expect(r.contentWidth).toBe(960 - SIDEBAR_DEFAULT - 1);
  });

  it("覆盖式面板宽度不会超出窗口，也不会窄于最小宽度", () => {
    const narrow = computeLayout({
      windowWidth: 960,
      prefs: { ...defaultPrefs(), inspectorOpen: true, inspector: INSPECTOR_MAX },
    });
    expect(narrow.inspector).toBeGreaterThanOrEqual(INSPECTOR_MIN);
    expect(narrow.inspector).toBeLessThanOrEqual(960 - SIDEBAR_DEFAULT);

    const tiny = computeLayout({
      windowWidth: 700,
      prefs: { ...defaultPrefs(), inspectorOpen: true, inspector: INSPECTOR_MAX },
    });
    expect(tiny.inspector).toBeGreaterThanOrEqual(INSPECTOR_MIN);
    expect(tiny.contentWidth).toBeGreaterThan(0);
  });

  it("侧栏收起后中区变宽，且宽度夹在合法范围", () => {
    const wide = computeLayout({
      windowWidth: 1200,
      prefs: { ...defaultPrefs(), sidebarCollapsed: true, inspector: 9999 },
    });
    expect(wide.sidebar).toBe(0);
    expect(wide.inspector).toBeLessThanOrEqual(INSPECTOR_MAX);
    expect(wide.contentWidth).toBeGreaterThan(1200 - SIDEBAR_MAX);
  });

  it("侧栏宽度超出范围时被夹紧", () => {
    const a = computeLayout({ windowWidth: 1360, prefs: { ...defaultPrefs(), sidebar: 10 } });
    expect(a.sidebar).toBe(SIDEBAR_MIN);
    const b = computeLayout({ windowWidth: 1360, prefs: { ...defaultPrefs(), sidebar: 5000 } });
    expect(b.sidebar).toBe(SIDEBAR_MAX);
  });

  it("工具面板关闭时不占任何宽度，模式为 dock", () => {
    const r = computeLayout({ windowWidth: 1360, prefs: { ...defaultPrefs(), inspectorOpen: false } });
    expect(r.inspector).toBe(0);
    expect(r.inspectorMode).toBe("dock");
    expect(r.contentWidth).toBe(1360 - SIDEBAR_DEFAULT - 1);
  });

  it("窗口变化时不会把用户首选宽度写坏（纯函数不修改入参）", () => {
    const prefs = defaultPrefs();
    const snapshot = JSON.stringify(prefs);
    computeLayout({ windowWidth: 960, prefs });
    computeLayout({ windowWidth: 1920, prefs });
    expect(JSON.stringify(prefs)).toBe(snapshot);
  });
});

describe("layout：偏好迁移", () => {
  it("没有旧偏好时，新默认是工具面板收起", () => {
    const p = migratePrefs({});
    expect(p.inspectorOpen).toBe(false);
    expect(p.inspector).toBe(INSPECTOR_DEFAULT);
    expect(p.sidebar).toBe(SIDEBAR_DEFAULT);
    expect(p.sidebarCollapsed).toBe(false);
    expect(p.version).toBe(1);
  });

  it("旧 ee.right=1 的用户保留展开偏好", () => {
    expect(migratePrefs({ right: "1" }).inspectorOpen).toBe(true);
    expect(migratePrefs({ right: "0" }).inspectorOpen).toBe(false);
  });

  it("旧 ee.rightWidth 在合法区间内被保留并夹到新范围", () => {
    expect(migratePrefs({ rightWidth: "480" }).inspector).toBe(480);
    // 旧范围允许 320，新最小 360：夹到 360 而不是丢弃
    expect(migratePrefs({ rightWidth: "320" }).inspector).toBe(INSPECTOR_MIN);
    expect(migratePrefs({ rightWidth: "820" }).inspector).toBe(INSPECTOR_MAX);
  });

  it("损坏或越界的旧值回退到默认，不抛错", () => {
    for (const bad of ["", "abc", "NaN", "-40", "0", null, undefined]) {
      const p = migratePrefs({ rightWidth: bad as never });
      expect(p.inspector).toBe(INSPECTOR_DEFAULT);
    }
    expect(migratePrefs({ right: "yes" }).inspectorOpen).toBe(false);
  });

  it("已经是 v1 的偏好原样沿用，不重复迁移", () => {
    const v1 = { ...defaultPrefs(), sidebar: 260, inspector: 500, inspectorOpen: true };
    expect(migratePrefs({ layout: JSON.stringify(v1) })).toEqual(v1);
  });

  it("损坏的新偏好回退默认而不是崩溃", () => {
    expect(migratePrefs({ layout: "{not json" })).toEqual(defaultPrefs());
    expect(migratePrefs({ layout: JSON.stringify({ version: 1, sidebar: "x" }) })).toEqual(defaultPrefs());
  });
});
