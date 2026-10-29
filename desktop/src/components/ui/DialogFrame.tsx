// 对话框统一外壳（Wave 5）。
//
// 改版前四个业务对话框各写一份 Dialog.Content，位置和最大高度互不相同
// （top 10% / 12% / 14%，max-h 72–78vh，宽度 560/620/640），弹窗跳来跳去。
// 这里只统一"外壳"：定位、圆角、宽度档位、高度上限、标题行结构。
// 各对话框的业务表单、RPC 调用、状态一律不动。

import * as Dialog from "@radix-ui/react-dialog";
import clsx from "clsx";
import { X } from "lucide-react";
import React from "react";
import { Button } from "../ui";

export type DialogWidth = "sm" | "md" | "lg";

const WIDTH: Record<DialogWidth, string> = {
  sm: "w-[520px]",
  md: "w-[620px]",
  lg: "w-[720px]",
};

/**
 * 统一外壳。调用方只提供标题、图标和正文；
 * 打开状态仍由各自的 store 字段控制，不自建 open 状态。
 */
export function DialogFrame({
  title,
  icon,
  onClose,
  width = "md",
  closeLabel = "关闭",
  children,
}: {
  title: React.ReactNode;
  icon?: React.ReactNode;
  onClose: () => void;
  width?: DialogWidth;
  closeLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40 fadein" />
        <Dialog.Content
          aria-describedby={undefined}
          // 统一从 top-[10%] 起、统一 max-h-[78vh]：切换不同对话框时不再上下跳。
          className={clsx(
            "fixed left-1/2 top-[10%] z-50 flex max-h-[78vh] -translate-x-1/2 flex-col rounded-2xl border border-line bg-elev p-4 shadow-[var(--shadow)] fadein",
            WIDTH[width],
            "max-w-[94vw]",
          )}
          data-testid="dialog-frame"
        >
          <div className="mb-3 flex shrink-0 items-center gap-2">
            {icon && <span className="flex shrink-0 items-center text-muted">{icon}</span>}
            <Dialog.Title className="min-w-0 flex-1 truncate text-[14px] font-semibold text-fg">{title}</Dialog.Title>
            <Dialog.Close asChild>
              <Button size="icon-sm" aria-label={closeLabel} className="ee-focus">
                <X size={14} />
              </Button>
            </Dialog.Close>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
