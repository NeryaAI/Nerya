"use client";

import { useMemo, type ReactNode } from "react";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { useTranslations } from "next-intl";
import { CheckIcon, ChevronDownIcon } from "./icons";

export interface SelectOption<T extends string = string> {
  value: T;
  label: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
}
interface SelectProps<T extends string = string> {
  value: T | null | undefined;
  onChange: (value: T) => void;
  options: SelectOption<T>[];
  placeholder?: ReactNode;
  className?: string;
  disabled?: boolean;
  id?: string;
  ariaLabel?: string;
  panelWidth?: number | string;
  align?: "left" | "right";
  size?: "sm" | "md";
  renderTrigger?: (active: SelectOption<T> | null) => ReactNode;
}

/** Backwards-compatible form API, backed by Radix's single-choice menu.
 * Radix owns arrow navigation, typeahead, disabled items, collision and focus.
 * Keep its native menu/radio semantics instead of nesting buttons in a listbox.
 */
export function Select<T extends string = string>({
  value, onChange, options, placeholder, className, disabled, id, ariaLabel,
  panelWidth, align = "left", size = "md", renderTrigger,
}: SelectProps<T>) {
  const t = useTranslations("ui");
  const active = useMemo(() => options.find((option) => option.value === value) ?? null, [options, value]);
  return (
    <Menu.Root>
      <Menu.Trigger asChild>
        <button
          type="button"
          id={id}
          aria-label={ariaLabel}
          disabled={disabled}
          className={[
            "group inline-flex w-full min-w-0 items-center justify-between gap-2 rounded-lg border border-[color:var(--line)] bg-[color:var(--card-hi)] text-[color:var(--text-base)] transition-colors",
            "hover:border-[color:var(--line-hi)] data-[state=open]:border-brand-500/55 disabled:cursor-not-allowed disabled:opacity-60",
            size === "sm" ? "min-h-8 px-2.5 text-xs" : "min-h-9 px-3 text-[13px]",
            className ?? "",
          ].join(" ")}
        >
          <span className="min-w-0 flex-1 truncate text-left">
            {renderTrigger ? renderTrigger(active) : active ? active.label : <span className="text-[color:var(--text-muted)]">{placeholder ?? "–"}</span>}
          </span>
          <ChevronDownIcon size={14} className="shrink-0 transition-transform group-data-[state=open]:rotate-180" />
        </button>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content
          align={align === "right" ? "end" : "start"}
          sideOffset={6}
          collisionPadding={8}
          className="ui-select-menu"
          style={{ width: panelWidth ?? "var(--radix-dropdown-menu-trigger-width)" }}
          aria-label={ariaLabel}
        >
          {options.length ? (
            <Menu.RadioGroup value={value ?? ""} onValueChange={(next) => onChange(next as T)}>
              {options.map((option) => (
                <Menu.RadioItem
                  key={option.value}
                  value={option.value}
                  disabled={option.disabled}
                  textValue={typeof option.label === "string" ? option.label : undefined}
                  className="ui-select-option"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block break-words">{option.label}</span>
                    {option.description ? <span className="mt-0.5 block text-xs leading-relaxed text-[color:var(--text-muted)]">{option.description}</span> : null}
                  </span>
                  <span className="flex w-4 shrink-0 items-center justify-center"><Menu.ItemIndicator><CheckIcon size={14} /></Menu.ItemIndicator></span>
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
          ) : <div className="px-3 py-3 text-sm text-[color:var(--text-muted)]">{t("noOptions")}</div>}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
