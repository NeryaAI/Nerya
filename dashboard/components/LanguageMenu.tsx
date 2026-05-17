"use client";

/**
 * Top-bar language menu.
 *
 * Replaces the binary EN/中文 toggle with a real dropdown so we can
 * grow the locale list without crowding the bar. Uses the shared
 * ``PortalDropdown`` so the menu escapes the rail's
 * ``overflow-x-auto`` viewport.
 */

import { useRef } from "react";
import { useUiSettings } from "../lib/settings";
import { LOCALE_LABELS, LOCALES, type Locale } from "../lib/i18n";
import { CheckIcon, ChevronDownIcon, LanguagesIcon } from "./icons";
import { PortalDropdown, useDropdown } from "./PortalDropdown";

const LOCALE_HINTS: Record<Locale, string> = {
  en: "EN",
  zh: "中",
};

export function LanguageMenu() {
  const [settings, patch] = useUiSettings();
  const current: Locale = settings.language === "zh" ? "zh" : "en";
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const { open, toggle, close } = useDropdown();

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={toggle}
        className="topnav-pill-icon cursor-pointer flex items-center gap-1 px-2"
        title={`Language · ${LOCALE_LABELS[current]}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <LanguagesIcon size={16} />
        <span className="text-[10px] font-mono font-semibold tracking-wider text-ink-200">
          {LOCALE_HINTS[current]}
        </span>
        <ChevronDownIcon
          size={10}
          className={`opacity-60 transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      <PortalDropdown
        open={open}
        onClose={close}
        anchorRef={triggerRef}
        align="right"
        width={196}
      >
        <div className="px-2 pt-1 pb-1.5 text-[11px] font-medium text-ink-400">
          Language
        </div>
        {LOCALES.map((locale) => {
          const active = current === locale;
          return (
            <button
              key={locale}
              type="button"
              role="menuitem"
              onClick={() => {
                patch({ language: locale });
                close();
              }}
              className={`w-full flex items-center justify-between gap-3 rounded-xl px-3 py-2 text-[13px] cursor-pointer transition-colors ${
                active
                  ? "bg-brand-500/15 text-white"
                  : "text-ink-200 hover:bg-brand-500/10 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2 min-w-0">
                <span className="font-mono text-[10px] font-semibold tracking-wider text-ink-300 w-6 text-center">
                  {LOCALE_HINTS[locale]}
                </span>
                <span className="truncate">{LOCALE_LABELS[locale]}</span>
              </span>
              {active ? (
                <CheckIcon size={14} className="text-brand-300 shrink-0" />
              ) : null}
            </button>
          );
        })}
      </PortalDropdown>
    </>
  );
}

export default LanguageMenu;
