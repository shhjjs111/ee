// 应用壳：唯一决定「侧栏 / 中区 / 工具面板」三者宽度与展现方式的地方。
//
// 布局计算全部来自 lib/layout.ts 的纯函数（有单测）；这里只做三件事：
// 1) 监听窗口 resize 写进 layoutStore；
// 2) 按计算结果设置宽度（停靠）或覆盖（overlay）；
// 3) 保证覆盖式面板不遮挡右上角窗口按钮。

import clsx from "clsx";
import React, { useEffect } from "react";
import { useLayout } from "../../lib/layoutStore";

export function AppShell({
  sidebar,
  main,
  inspector,
}: {
  /** 传 null 表示这一页不要侧栏（如设置页自带导航，不该再叠一层项目树）。 */
  sidebar: React.ReactNode | null;
  main: React.ReactNode;
  inspector: React.ReactNode | null;
}) {
  const layout = useLayout((s) => s.layout);
  const setWindowWidth = useLayout((s) => s.setWindowWidth);

  useEffect(() => {
    const onResize = () => setWindowWidth(window.innerWidth);
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [setWindowWidth]);

  const overlay = inspector !== null && layout.inspectorMode === "overlay";
  const showSidebar = sidebar !== null && layout.sidebar > 0;

  return (
    <div className="relative flex h-full overflow-hidden" data-testid="app-shell" data-inspector-mode={layout.inspectorMode}>
      {showSidebar && (
        <div className="flex shrink-0 flex-col border-r border-line" style={{ width: layout.sidebar }} data-testid="shell-sidebar">
          {sidebar}
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">{main}</div>
      {inspector !== null &&
        (overlay ? (
          // 覆盖式：贴着右侧但不越过窗口按钮区（右上 44px 那条留给系统）
          <div
            className="absolute bottom-0 right-0 top-0 z-30 flex flex-col border-l border-line bg-bg shadow-[var(--shadow)]"
            style={{ width: layout.inspector }}
            data-testid="shell-inspector"
          >
            {inspector}
          </div>
        ) : (
          <div
            className={clsx("flex shrink-0 flex-col border-l border-line bg-bg")}
            style={{ width: layout.inspector }}
            data-testid="shell-inspector"
          >
            {inspector}
          </div>
        ))}
    </div>
  );
}
