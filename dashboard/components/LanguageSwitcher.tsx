"use client";

import { useTranslations } from "next-intl";
import { useUiSettings } from "../lib/settings";
import { LOCALE_LABELS, LOCALES } from "../lib/i18n";

export function LanguageSwitcher({ collapsed }: { collapsed?: boolean }) {
  const t = useTranslations("languageSwitcher");
  const [settings, patch] = useUiSettings();
  const current = settings.language === "zh" ? "zh" : "en";

  if (collapsed) {
    return (
      <button
        onClick={() => patch({ language: current === "zh" ? "en" : "zh" })}
        className="w-full flex items-center justify-center rounded-lg px-0 py-2 text-xs text-ink-300 hover:bg-brand-500/10 hover:text-white transition-colors"
        title={current === "zh" ? t("switchToEnglish") : t("switchToChinese")}
      >
        <span className="font-mono text-[11px] font-semibold">
          {current.toUpperCase()}
        </span>
      </button>
    );
  }

  return (
    <div className="flex items-center gap-1 rounded-lg border border-brand-500/15 bg-ink-900/40 p-0.5">
      {LOCALES.map((locale) => (
        <button
          key={locale}
          onClick={() => patch({ language: locale })}
          className={[
            "flex-1 rounded-md px-2.5 py-1 text-[11px] font-semibold transition-all",
            current === locale
              ? "bg-brand-500 text-white shadow-glow"
              : "text-ink-400 hover:text-ink-100",
          ].join(" ")}
        >
          {LOCALE_LABELS[locale]}
        </button>
      ))}
    </div>
  );
}
