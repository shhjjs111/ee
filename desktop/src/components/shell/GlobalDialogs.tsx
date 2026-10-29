// 全局对话框挂载点：改版前 Worktree/Export/Handoff/Bestof 四个对话框挂在 Sidebar 内部
// （Sidebar.tsx 末尾）。侧栏一旦折叠或换成 rail，这些入口就会跟着消失。
// 这里把它们统一挂到应用根部，与对话框的业务 state key 完全一致。

import { BestofDialog } from "../BestofDialog";
import { ExportDialog } from "../ExportDialog";
import { HandoffDialog } from "../HandoffDialog";
import { WorktreeDialog } from "../WorktreeDialog";
import { Dialogs } from "../ReviewDialogs";

export function GlobalDialogs() {
  return (
    <>
      <WorktreeDialog />
      <ExportDialog />
      <HandoffDialog />
      <BestofDialog />
      <Dialogs />
    </>
  );
}
