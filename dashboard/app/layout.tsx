import "./globals.css";
import type { Metadata } from "next";
import { ThemeApplier } from "../components/ThemeApplier";
import { I18nProvider } from "../components/I18nProvider";
import { AppShell } from "../components/landing/AppShell";

export const metadata: Metadata = {
  title: "Nerya · Self-evolving trading agent",
  description:
    "Skill-first, trading-native, self-evolving autonomous agent runtime.",
  icons: {
    icon: [
      { url: "/branding/svg/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/branding/svg/favicon-32x32.png", sizes: "32x32", type: "image/png" },
    ],
    shortcut: "/branding/svg/favicon.ico",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-ink-950 text-ink-100 font-sans antialiased">
        {/* 根据 localStorage 设置同步 html 上的 light/dark class */}
        <ThemeApplier />
        <I18nProvider>
          <AppShell>{children}</AppShell>
        </I18nProvider>
      </body>
    </html>
  );
}
