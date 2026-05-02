"use client";

import { NextIntlClientProvider } from "next-intl";
import { useUiSettings } from "../lib/settings";
import en from "../messages/en.json";
import zh from "../messages/zh.json";

const messages = { en, zh } as const;

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [settings] = useUiSettings();
  const locale = settings.language === "zh" ? "zh" : "en";
  return (
    <NextIntlClientProvider
      key={locale}
      locale={locale}
      messages={messages[locale]}
    >
      {children}
    </NextIntlClientProvider>
  );
}
