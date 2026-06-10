"use client";

/**
 * Custom <Select> built on PortalDropdown.
 *
 * Replaces native ``<select>`` elements which inherit OS chrome that
 * clashes with the airy violet-glass design. The portal anchor avoids
 * any clipping issues from ``overflow-x-auto`` / ``overflow-hidden``
 * on parent layout shells.
 *
 * The trigger and option list both share the airy violet/glass tokens
 * used elsewhere on the dashboard so it slots into existing forms
 * without any custom alignment.
 */

import {
  ReactNode,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ChevronDownIcon } from "./icons";
import { PortalDropdown, useDropdown } from "./PortalDropdown";

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

const SIZE_CLASSES: Record<NonNullable<SelectProps["size"]>, string> = {
  sm: "h-8 px-2.5 text-[12px]",
  md: "h-9 px-3 text-[13px]",
};

export function Select<T extends string = string>({
  value,
  onChange,
  options,
  placeholder,
  className,
  disabled,
  id,
  ariaLabel,
  panelWidth,
  align = "left",
  size = "md",
  renderTrigger,
}: SelectProps<T>) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dropdown = useDropdown();
  const active = useMemo(
    () => options.find((option) => option.value === value) ?? null,
    [options, value],
  );
  const sizeCls = SIZE_CLASSES[size];

  const [autoWidth, setAutoWidth] = useState<number>(220);
  useLayoutEffect(() => {
    if (panelWidth || !triggerRef.current) return;
    const update = () => {
      const w = triggerRef.current?.offsetWidth;
      if (w && w > 0) setAutoWidth(w);
    };
    update();
    if (typeof ResizeObserver !== "undefined") {
      const obs = new ResizeObserver(update);
      obs.observe(triggerRef.current);
      return () => obs.disconnect();
    }
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [panelWidth]);

  // Track open transitions so we re-measure right before opening.
  useEffect(() => {
    if (!dropdown.open) return;
    if (panelWidth || !triggerRef.current) return;
    const w = triggerRef.current.offsetWidth;
    if (w && w > 0) setAutoWidth(w);
  }, [dropdown.open, panelWidth]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        aria-haspopup="listbox"
        aria-expanded={dropdown.open}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => {
          if (!disabled) dropdown.toggle();
        }}
        className={[
          "inline-flex w-full items-center justify-between gap-2 rounded-lg",
          "border border-brand-500/15 bg-ink-900/40 hover:border-brand-500/35",
          "text-ink-100 transition-colors backdrop-blur-soft",
          dropdown.open ? "border-brand-500/45 bg-ink-900/55" : "",
          disabled ? "opacity-60 cursor-not-allowed" : "cursor-pointer",
          sizeCls,
          className ?? "",
        ].join(" ")}
      >
        <span className="min-w-0 flex-1 truncate text-left">
          {renderTrigger
            ? renderTrigger(active)
            : active
            ? active.label
            : (
              <span className="text-ink-500">{placeholder ?? "–"}</span>
            )}
        </span>
        <ChevronDownIcon
          size={14}
          className={[
            "shrink-0 text-ink-400 transition-transform",
            dropdown.open ? "rotate-180" : "",
          ].join(" ")}
        />
      </button>
      <PortalDropdown
        open={dropdown.open}
        onClose={dropdown.close}
        anchorRef={triggerRef}
        align={align}
        width={panelWidth ?? autoWidth}
        offset={6}
        className="max-h-72 overflow-y-auto rounded-xl border border-[color:var(--line)] bg-[color:var(--card)] py-1 shadow-[0_2px_8px_rgba(2,6,23,0.18)]"
      >
        <ul role="listbox" className="text-[13px]">
          {options.length === 0 ? (
            <li className="px-3 py-2 text-[12px] text-ink-500 italic">
              No options
            </li>
          ) : (
            options.map((option) => {
              const selected = option.value === value;
              return (
                <li key={option.value} role="option" aria-selected={selected}>
                  <button
                    type="button"
                    disabled={option.disabled}
                    onClick={() => {
                      if (option.disabled) return;
                      onChange(option.value);
                      dropdown.close();
                    }}
                    className={[
                      "flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors",
                      option.disabled
                        ? "cursor-not-allowed opacity-50"
                        : "cursor-pointer hover:bg-brand-500/12",
                      selected ? "bg-brand-500/14 text-white" : "text-ink-200",
                    ].join(" ")}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {option.label}
                    </span>
                    {selected ? (
                      <span className="mt-[2px] h-1.5 w-1.5 rounded-full bg-brand-300" />
                    ) : null}
                  </button>
                  {option.description ? (
                    <div className="px-3 pb-1.5 text-[11px] text-ink-500">
                      {option.description}
                    </div>
                  ) : null}
                </li>
              );
            })
          )}
        </ul>
      </PortalDropdown>
    </>
  );
}
