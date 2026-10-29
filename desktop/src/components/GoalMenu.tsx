// 「目标」入口（T10-b 界面侧）：Codex 式两级交互。
//   L1：`+` 菜单分组，每项带图标 + 标题 + 灰色描述
//   L2：点「目标」从菜单原位换成输入态，填判据与轮次
// 视觉沿用 ee 现有设计系统（--elev/--line/--hover/--accent + ui.tsx 的 Button/Input）。
import * as DM from "@radix-ui/react-dropdown-menu";
import { Check, Paperclip, Plus, Target, X } from "lucide-react";
import { useState } from "react";
import { Button, Input } from "./ui";

export interface GoalRequest {
  command: string;
  maxRounds: number;
}

export function GoalMenu({
  onSubmit,
  defaultRounds = 3,
  disabled,
  onPickFiles,
}: {
  onSubmit: (req: GoalRequest) => void;
  defaultRounds?: number;
  disabled?: boolean;
  /** 点「文件和文件夹」时调用；不给就不显示这一项。 */
  onPickFiles?: () => void;
}) {
  const [mode, setMode] = useState<"menu" | "goal">("menu");
  const [open, setOpen] = useState(false);
  const [command, setCommand] = useState("");
  const [rounds, setRounds] = useState(defaultRounds);

  const close = () => {
    setOpen(false);
    setMode("menu");
  };

  const startGoal = () => {
    const cmd = command.trim();
    if (!cmd) return;                       // 空判据不提交：没有判据的循环没有意义
    onSubmit({ command: cmd, maxRounds: rounds });
    setCommand("");
    close();
  };

  return (
    <DM.Root open={open} onOpenChange={(o) => (o ? setOpen(true) : close())}>
      <DM.Trigger asChild>
        <button
          className="flex h-7 w-7 items-center justify-center rounded-lg text-muted transition-colors hover:bg-hover hover:text-fg disabled:opacity-40"
          disabled={disabled}
          title="添加（文件、目标）"
          aria-label="添加"
        >
          <Plus size={15} />
        </button>
      </DM.Trigger>
      <DM.Portal>
        <DM.Content
          align="start"
          side="top"
          sideOffset={8}
          className="z-50 w-[380px] rounded-xl border border-line bg-elev p-1.5 shadow-[var(--shadow)] fadein"
        >
          {mode === "menu" ? (
            <>
              <DM.Label className="px-2.5 pb-1 pt-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">
                添加
              </DM.Label>
              {onPickFiles && (
                <DM.Item
                  onSelect={(e) => {
                    e.preventDefault();
                    onPickFiles();
                    close();
                  }}
                  className="flex cursor-default items-start gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] text-fg outline-none data-[highlighted]:bg-hover"
                >
                  <Paperclip size={15} className="mt-0.5 shrink-0 text-muted" />
                  <span className="min-w-0 flex-1">
                    <span className="block">文件和文件夹</span>
                    <span className="mt-0.5 block text-[11.5px] leading-snug text-faint">
                      把文件或目录加入这次对话的上下文
                    </span>
                  </span>
                </DM.Item>
              )}
              <DM.Item
                onSelect={(e) => {
                  e.preventDefault();          // 不关菜单，原位换成输入态
                  setMode("goal");
                }}
                className="flex cursor-default items-start gap-2.5 rounded-lg px-2.5 py-1.5 text-[13px] text-fg outline-none data-[highlighted]:bg-hover"
              >
                <Target size={15} className="mt-0.5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1">
                  <span className="block">目标</span>
                  <span className="mt-0.5 block text-[11.5px] leading-snug text-faint">
                    设置要持续追求的目标
                  </span>
                </span>
              </DM.Item>
            </>
          ) : (
            <div className="p-2">
              <div className="mb-1 flex items-center gap-2">
                <Target size={14} className="text-accent" />
                <span className="flex-1 text-[13px] font-medium text-fg">要持续追求的目标</span>
                <button
                  className="flex h-5 w-5 items-center justify-center rounded text-faint hover:bg-hover hover:text-fg"
                  onClick={close}
                  aria-label="返回"
                >
                  <X size={13} />
                </button>
              </div>
              <div className="mb-2.5 text-[11.5px] leading-relaxed text-faint">
                ee 反复尝试直到这条命令判定通过。要求退出码为 0 且确实收集到测试：
                零测试、超时都不算通过。
              </div>
              <Input
                autoFocus
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) startGoal();
                  if (e.key === "Escape") close();
                }}
                placeholder="例如 pytest -q"
                className="mb-2"
              />
              <div className="mb-2.5 flex items-center gap-1.5 text-[11.5px] text-faint">
                <span>最多</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={rounds}
                  onChange={(e) => setRounds(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
                  className="h-6 w-12 rounded-md border border-line bg-bg px-1.5 text-[11.5px] text-fg outline-none focus:border-accent"
                />
                <span>轮判定</span>
              </div>
              <div className="flex items-center gap-2">
                <Button size="sm" variant="primary" onClick={startGoal} disabled={!command.trim()}>
                  <Check size={13} /> 开始
                </Button>
                <Button size="sm" variant="ghost" onClick={close}>
                  取消
                </Button>
              </div>
            </div>
          )}
        </DM.Content>
      </DM.Portal>
    </DM.Root>
  );
}
