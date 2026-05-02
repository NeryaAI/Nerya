"use client";

import { NextIntlClientProvider } from "next-intl";
import { useUiSettings } from "../lib/settings";
import en from "../messages/en.json";
import zh from "../messages/zh.json";

const messages = { en, zh } as const;

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [settings] = useUiSettings();
  const locale = settings.language === "zh" ? "zh" : "en";
  const onError = (error: { code?: unknown }) => {
    const code = String(error?.code ?? "");
    if (code === "MISSING_MESSAGE" || code === "ENVIRONMENT_FALLBACK") {
      return;
    }
    console.error(error);
  };

  return (
    <NextIntlClientProvider
      key={locale}
      locale={locale}
      messages={messages[locale]}
      onError={onError}
      getMessageFallback={({ namespace, key }) =>
        namespace ? `${namespace}.${key}` : key
      }
    >
      {children}
    </NextIntlClientProvider>
  );
}
