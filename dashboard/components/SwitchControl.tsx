"use client";

import { useTranslations } from "next-intl";

type SwitchTone = "brand" | "accent" | "danger";
type SwitchSize = "sm" | "md";

const toneClasses: Record<SwitchTone, string> = {
  brand: "border-brand-400/45 bg-brand-500 shadow-[0_0_18px_-8px_rgba(139,92,246,0.85)]",
  accent: "border-accent-400/45 bg-accent-500/80 shadow-[0_0_18px_-8px_rgba(16,217,147,0.85)]",
  danger: "border-[#ef4560]/55 bg-[#ef4560] shadow-[0_0_18px_-8px_rgba(239,69,96,0.9)]",
};

const mutedToneClasses: Record<SwitchTone, string> = {
  brand: "border-brand-400/35 bg-brand-500/15",
  accent: "border-accent-400/35 bg-accent-500/15",
  danger: "border-[#ef4560]/45 bg-[#ef4560]/15",
};

const sizeClasses: Record<SwitchSize, { root: string; thumb: string; checked: string }> = {
  sm: {
    root: "h-5 w-9",
    thumb: "h-4 w-4",
    checked: "translate-x-4",
  },
  md: {
    root: "h-6 w-11",
    thumb: "h-5 w-5",
    checked: "translate-x-5",
  },
};

function switchTrackClass({
  checked,
  disabled,
  interactive,
  toneWhenOff,
  size,
  tone,
}: {
  checked: boolean;
  disabled?: boolean;
  interactive: boolean;
  toneWhenOff?: boolean;
  size: SwitchSize;
  tone: SwitchTone;
}) {
  return [
    "relative inline-flex shrink-0 items-center overflow-hidden rounded-full border p-0.5 align-middle transition",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fluid-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0a0b1a]",
    sizeClasses[size].root,
    checked ? toneClasses[tone] : toneWhenOff ? mutedToneClasses[tone] : "border-ink-600 bg-ink-800/95 shadow-inner",
    interactive && !disabled ? "cursor-pointer hover:border-brand-300/60" : "cursor-default",
    disabled ? "cursor-not-allowed opacity-55" : "",
  ].join(" ");
}

function switchThumbClass(checked: boolean, size: SwitchSize) {
  return [
    "pointer-events-none block rounded-full bg-white ring-1 ring-black/10 transition-transform duration-200 ease-out",
    "shadow-[0_2px_8px_rgba(0,0,0,0.38)]",
    sizeClasses[size].thumb,
    checked ? sizeClasses[size].checked : "translate-x-0",
  ].join(" ");
}

export function SwitchControl({
  checked,
  onCheckedChange,
  disabled = false,
  label,
  tone = "brand",
  size = "md",
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
  tone?: SwitchTone;
  size?: SwitchSize;
}) {
  const t = useTranslations("pageCommon");
  const ariaLabel = label ?? t("toggleSetting");
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={switchTrackClass({ checked, disabled, interactive: true, size, tone })}
    >
      <span aria-hidden="true" className={switchThumbClass(checked, size)} />
    </button>
  );
}

export function SwitchIndicator({
  checked,
  label,
  tone = "accent",
  size = "sm",
  toneWhenOff = false,
}: {
  checked: boolean;
  label: string;
  tone?: SwitchTone;
  size?: SwitchSize;
  toneWhenOff?: boolean;
}) {
  return (
    <span
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={switchTrackClass({ checked, interactive: false, toneWhenOff, size, tone })}
    >
      <span aria-hidden="true" className={switchThumbClass(checked, size)} />
    </span>
  );
}
