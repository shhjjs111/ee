// 标题栏：Electron 用 titleBarStyle:"hidden" + titleBarOverlay（高度 44）时，
// 右上角是系统画的窗口按钮。改版前每个页面各自 h-11 + padding，容易把按钮压住。
// 现在只有这里决定标题栏高度与右侧安全区，各页面只放内容。

import clsx from "clsx";
import React from "react";

export const TITLEBAR_H = 44;

/**
 * 右侧安全区：Windows 上窗口按钮区宽约 138px（App 写入 --overlay-w）；
 * macOS 上是红绿灯左侧留白（--traffic-w）。两个变量由 App 按平台设置。
 */
export function Titlebar({
  children,
  right,
  className,
  /** 侧栏折叠按钮等需要可点击的元素放这里，自动加 no-drag。 */
  leading,
}: {
  children?: React.ReactNode;
  right?: React.ReactNode;
  className?: string;
  leading?: React.ReactNode;
}) {
  return (
    <div
      className={clsx("drag flex shrink-0 items-center gap-2 border-b border-line px-3", className)}
      style={{
        height: TITLEBAR_H,
        paddingLeft: "calc(var(--traffic-w, 16px) + 8px)",
        // 有 right 时由它自己贴着窗口按钮左侧；没有则留出系统按钮区，避免内容被压住。
        paddingRight: right ? 8 : "calc(var(--overlay-w, 0px) + 8px)",
      }}
      data-testid="titlebar"
    >
      {leading && <div className="no-drag flex shrink-0 items-center">{leading}</div>}
      <div className="flex min-w-0 flex-1 items-center gap-2">{children}</div>
      {right && <div className="no-drag flex shrink-0 items-center gap-1">{right}</div>}
    </div>
  );
}
