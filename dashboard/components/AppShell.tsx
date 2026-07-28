"use client";

import { usePathname } from "next/navigation";
import { AuthGate } from "./AuthGate";
import { PageTransition } from "./PageTransition";
import { CodexSidebar } from "./shell/CodexSidebar";
import { SettingsSidebar } from "./shell/SettingsSidebar";
import { ShellNotifications } from "./shell/ShellNotifications";
import { CommandPaletteProvider } from "./shell/CommandPalette";

// Settings surfaces take over the left rail with the Codex-style
// settings navigation (SettingsSidebar). These are `/settings` plus the
// standalone "More" pages that are really settings sections rendered via
// SettingsWorkspace `forceSection` (and the Gateway ops page).
const SETTINGS_SURFACES = [
  "/settings",
  "/memory",
  "/web-search",
  "/browsers",
  "/env-vault",
  "/gateway",
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLogin = pathname === "/login";
  const isEmbeddedSurface = pathname?.startsWith("/browser-session/embed");

  if (isLogin || isEmbeddedSurface) {
    return <>{children}</>;
  }

  // Full-bleed surfaces own the whole viewport (no page padding, no
  // max-width clamp): the chat workspace and the Codex-style command home.
  const isFullBleed =
    pathname === "/" || pathname === "/chat" || pathname?.startsWith("/chat/");

  const isSettingsSurface = SETTINGS_SURFACES.some(
    (p) => pathname === p || pathname?.startsWith(`${p}/`),
  );
  const pageFrameClass = isSettingsSurface
    ? "mx-auto w-full max-w-[1120px] px-5 pb-10 pt-1 lg:px-8"
    : // 1360px keeps tables readable on ultrawide monitors — at 1500px the
      // 6-column strategy/portfolio tables spread so far apart the eye has
      // to jump between columns.
      "mx-auto w-full max-w-[1360px] px-4 pb-12 pt-2 lg:px-8";

  return (
    <AuthGate>
      <CommandPaletteProvider>
        <div className={`nerya-app-shell ${isSettingsSurface ? "nerya-settings-shell" : ""} flex h-screen min-h-0 overflow-hidden`}>
          {isSettingsSurface ? <SettingsSidebar /> : <CodexSidebar />}
          <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
            <div className="grid-bg pointer-events-none absolute inset-0 opacity-[0.06]" />
            {/* Top-right shell chrome: notifications live here (Codex
                keeps them out of the rail). */}
            <div className="relative z-30 flex h-12 shrink-0 items-center justify-end gap-2 px-3">
              <ShellNotifications />
            </div>
            {isFullBleed ? (
              <div className="relative flex min-h-0 flex-1 flex-col">{children}</div>
            ) : (
              <div className="relative flex min-h-0 flex-1 flex-col overflow-y-auto">
                <div className={pageFrameClass}>
                  <PageTransition>{children}</PageTransition>
                </div>
              </div>
            )}
          </main>
        </div>
      </CommandPaletteProvider>
    </AuthGate>
  );
}
