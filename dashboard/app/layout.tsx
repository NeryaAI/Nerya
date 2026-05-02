import "./globals.css";
import type { Metadata } from "next";
import { Sidebar } from "../components/Sidebar";
import { TopHeader } from "../components/TopHeader";
import { ThemeApplier } from "../components/ThemeApplier";
import { PageTransition } from "../components/PageTransition";
import { I18nProvider } from "../components/I18nProvider";

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
          <div className="flex min-h-screen">
            <Sidebar />
            <main className="flex-1 min-w-0 flex flex-col">
              <TopHeader />
              <div className="relative flex-1">
                <div className="absolute inset-0 grid-bg opacity-20 pointer-events-none" />
                <div
                  className="absolute inset-0 pointer-events-none"
                  style={{
                    background:
                      "radial-gradient(75% 60% at 50% 0%, rgba(139,92,246,0.06), transparent 70%)",
                  }}
                />
                <div className="relative px-6 lg:px-10 py-6 max-w-[1500px] mx-auto">
                  <PageTransition>{children}</PageTransition>
                </div>
              </div>
            </main>
          </div>
        </I18nProvider>
      </body>
    </html>
  );
}
