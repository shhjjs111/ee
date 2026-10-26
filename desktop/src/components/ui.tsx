import * as DM from "@radix-ui/react-dropdown-menu";
import * as TT from "@radix-ui/react-tooltip";
import clsx from "clsx";
import { Check, ChevronDown } from "lucide-react";
import React from "react";

export function Tip({ label, children, side = "top" }: { label: React.ReactNode; children: React.ReactElement; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <TT.Root delayDuration={350}>
      <TT.Trigger asChild>{children}</TT.Trigger>
      <TT.Portal>
        <TT.Content
          side={side}
          sideOffset={6}
          className="z-50 rounded-md bg-fg px-2 py-1 text-[12px] text-bg shadow-md fadein"
        >
          {label}
        </TT.Content>
      </TT.Portal>
    </TT.Root>
  );
}

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "outline" | "danger" | "soft";
  size?: "sm" | "md" | "icon" | "icon-sm";
};

export const Button = React.forwardRef<HTMLButtonElement, BtnProps>(function Button(
  { variant = "ghost", size = "md", className, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={clsx(
        "no-drag inline-flex select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-lg font-medium transition-colors disabled:pointer-events-none disabled:opacity-40",
        size === "md" && "h-8 px-3 text-[13px]",
        size === "sm" && "h-7 px-2.5 text-[12.5px]",
        size === "icon" && "h-8 w-8",
        size === "icon-sm" && "h-6 w-6 rounded-md",
        variant === "primary" && "bg-fg text-bg hover:opacity-85",
        variant === "ghost" && "text-muted hover:bg-hover hover:text-fg",
        variant === "soft" && "bg-soft text-fg hover:bg-hover",
        variant === "outline" && "border border-line-strong text-fg hover:bg-hover",
        variant === "danger" && "text-danger hover:bg-del-bg",
        className,
      )}
      {...rest}
    />
  );
});

export type Option = { value: string; label: string; hint?: string; icon?: React.ReactNode; disabled?: boolean };

export function Select({
  value,
  options,
  onChange,
  icon,
  className,
  title,
  align = "start",
  side = "top",
  renderLabel,
}: {
  value: string;
  options: Option[];
  onChange: (v: string) => void;
  icon?: React.ReactNode;
  className?: string;
  title?: string;
  align?: "start" | "end";
  side?: "top" | "bottom";
  renderLabel?: (o: Option | undefined) => React.ReactNode;
}) {
  const cur = options.find((o) => o.value === value);
  return (
    <DM.Root>
      <DM.Trigger asChild>
        <button
          title={title}
          className={clsx(
            "no-drag inline-flex h-7 max-w-[240px] items-center gap-1.5 rounded-lg px-2 text-[12.5px] text-muted outline-none transition-colors hover:bg-hover hover:text-fg data-[state=open]:bg-hover data-[state=open]:text-fg",
            className,
          )}
        >
          {icon}
          <span className="truncate">{renderLabel ? renderLabel(cur) : cur?.label ?? value}</span>
          <ChevronDown size={13} className="shrink-0 opacity-60" />
        </button>
      </DM.Trigger>
      <DM.Portal>
        <DM.Content
          side={side}
          align={align}
          sideOffset={6}
          className="z-50 max-h-[360px] min-w-[220px] overflow-y-auto rounded-xl border border-line bg-elev p-1 shadow-[var(--shadow)] fadein"
        >
          {options.map((o) => (
            <DM.Item
              key={o.value}
              disabled={o.disabled}
              onSelect={() => onChange(o.value)}
              className="flex cursor-default items-start gap-2 rounded-lg px-2.5 py-1.5 text-[13px] text-fg outline-none data-[disabled]:opacity-40 data-[highlighted]:bg-hover"
            >
              {o.icon && <span className="mt-0.5 text-muted">{o.icon}</span>}
              <span className="min-w-0 flex-1">
                <span className="block truncate">{o.label}</span>
                {o.hint && <span className="block text-[11.5px] leading-snug text-faint">{o.hint}</span>}
              </span>
              <Check size={14} className={clsx("mt-0.5 shrink-0", o.value === value ? "opacity-100" : "opacity-0")} />
            </DM.Item>
          ))}
        </DM.Content>
      </DM.Portal>
    </DM.Root>
  );
}

export function Menu({ trigger, items }: { trigger: React.ReactElement; items: { label: string; onSelect: () => void; danger?: boolean; icon?: React.ReactNode }[] }) {
  return (
    <DM.Root>
      <DM.Trigger asChild>{trigger}</DM.Trigger>
      <DM.Portal>
        <DM.Content
          align="end"
          sideOffset={4}
          className="z-50 min-w-[160px] rounded-xl border border-line bg-elev p-1 shadow-[var(--shadow)] fadein"
        >
          {items.map((it) => (
            <DM.Item
              key={it.label}
              onSelect={it.onSelect}
              className={clsx(
                "flex cursor-default items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] outline-none data-[highlighted]:bg-hover",
                it.danger ? "text-danger" : "text-fg",
              )}
            >
              {it.icon}
              {it.label}
            </DM.Item>
          ))}
        </DM.Content>
      </DM.Portal>
    </DM.Root>
  );
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="mono rounded border border-line bg-soft px-1 py-px text-[10.5px] text-muted">{children}</kbd>
  );
}

export function Field({ label, hint, children, className }: { label: string; hint?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <label className={clsx("block", className)}>
      <span className="mb-1 block text-[12px] font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11.5px] text-faint">{hint}</span>}
    </label>
  );
}

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      spellCheck={false}
      className={clsx(
        "h-8 w-full rounded-lg border border-line bg-bg px-2.5 text-[13px] text-fg outline-none transition-colors placeholder:text-faint focus:border-accent",
        className,
      )}
      {...rest}
    />
  );
});

export function Spinner({ size = 14, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={clsx("animate-spin", className)} fill="none">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      onClick={() => onChange(!on)}
      className={clsx("relative h-5 w-9 rounded-full transition-colors", on ? "bg-accent" : "bg-soft border border-line")}
    >
      <span className={clsx("absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all", on ? "left-[18px]" : "left-0.5")} />
    </button>
  );
}
