"use client";

import { usePathname } from "next/navigation";
import { AuthGate } from "../AuthGate";
import { TopNav } from "../TopNav";
import { PageTransition } from "../PageTransition";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLanding = pathname === "/";
  const isLogin = pathname === "/login";
  const isEmbeddedSurface = pathname?.startsWith("/browser-session/embed");
  const isChat = pathname?.startsWith("/chat");
  const contentClass = isChat
    ? "relative px-4 lg:px-8 pt-2 pb-0 max-w-none mx-auto"
    : "relative px-4 lg:px-8 pt-2 pb-10 max-w-[1500px] mx-auto";

  if (isLanding || isLogin || isEmbeddedSurface) {
    return <>{children}</>;
  }

  // Keep a single floating top nav across the dashboard shell. Chat still
  // keeps its own workspace layout, but the global menu remains visible
  // so operators do not lose primary navigation there.
  return (
    <AuthGate>
      <div className="nerya-app-shell min-h-screen flex flex-col">
        <TopNav />
        <main className="relative flex-1 min-w-0">
          <div className="absolute inset-0 grid-bg opacity-[0.12] pointer-events-none" />
          <div
            className="absolute inset-0 pointer-events-none"
            style={{
              background:
                "radial-gradient(75% 60% at 50% 0%, rgba(139,92,246,0.08), transparent 70%)",
            }}
          />
          <div className={contentClass}>
            <PageTransition>{children}</PageTransition>
          </div>
        </main>
      </div>
    </AuthGate>
  );
}
