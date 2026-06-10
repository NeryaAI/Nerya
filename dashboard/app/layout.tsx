import "./globals.css";
import type { Metadata } from "next";
import { ThemeApplier } from "../components/ThemeApplier";
import { I18nProvider } from "../components/I18nProvider";
import { AppShell } from "../components/AppShell";
import { DialogProvider } from "../lib/dialogs";

export const metadata: Metadata = {
  title: "Nerya · Self-evolving trading agent",
  description:
    "Skill-first, trading-native, self-evolving autonomous agent runtime.",
  icons: {
    icon: [
      { url: "/branding/Logo.png", sizes: "16x16", type: "image/png" },
      { url: "/branding/Logo.png", sizes: "32x32", type: "image/png" },
      { url: "/branding/Logo.png", sizes: "192x192", type: "image/png" },
    ],
    shortcut: "/branding/Logo.png",
    apple: [{ url: "/branding/Logo.png", sizes: "180x180", type: "image/png" }],
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
          <DialogProvider>
            <AppShell>{children}</AppShell>
          </DialogProvider>
        </I18nProvider>
      </body>
    </html>
  );
}
