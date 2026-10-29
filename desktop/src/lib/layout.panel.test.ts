// @vitest-environment jsdom
// 工具面板的宽度必须来自"真的打开了"这一状态（Wave 4 修掉的真实缺陷）。
//
// 缺陷现场：useApp.rightOpen 是工具面板开关的唯一真相，但 layoutStore 另存了
// 一份 inspectorOpen。两处不同步时，点开面板得到 width: 0 的容器——
// 实测在真实窗口里表现为「面板打开了，但只有 1px 宽」。
// 这里从纯函数入口钉住：inspectorOpen=false 时宽度必须是 0。
import { describe, expect, it } from "vitest";
import { INSPECTOR_DEFAULT, computeLayout, defaultPrefs } from "../lib/layout";

describe("工具面板宽度与开关一致", () => {
  it("开关关闭时宽度为 0（避免出现 1px 的幽灵面板）", () => {
    const r = computeLayout({ windowWidth: 1362, prefs: { ...defaultPrefs(), inspectorOpen: false } });
    expect(r.inspector).toBe(0);
  });

  it("开关打开且窗口够宽时，宽度回到首选值的合法区间内", () => {
    const r = computeLayout({ windowWidth: 1362, prefs: { ...defaultPrefs(), inspectorOpen: true } });
    expect(r.inspector).toBe(INSPECTOR_DEFAULT);
    expect(r.inspector).toBeGreaterThan(100);
    expect(r.inspectorMode).toBe("dock");
  });

  it("窗口窄到放不下时改为覆盖，宽度仍然是正常值而不是 0", () => {
    const r = computeLayout({ windowWidth: 960, prefs: { ...defaultPrefs(), inspectorOpen: true } });
    expect(r.inspectorMode).toBe("overlay");
    expect(r.inspector).toBeGreaterThanOrEqual(360);
  });
});
