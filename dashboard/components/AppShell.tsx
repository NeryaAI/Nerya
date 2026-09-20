"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import * as Dialog from "@radix-ui/react-dialog";
import { AuthGate } from "./AuthGate";
import { PageTransition } from "./PageTransition";
import { CodexSidebar } from "./shell/CodexSidebar";
import { SettingsSidebar } from "./shell/SettingsSidebar";
import { ShellNotifications } from "./shell/ShellNotifications";
import { ShellNavigationTrigger } from "./shell/ShellNavigationTrigger";
import { CommandPaletteProvider } from "./shell/CommandPalette";
import { XIcon } from "./icons";

const SETTINGS_SURFACES = ["/settings", "/memory", "/web-search", "/browsers", "/env-vault", "/gateway"];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() || "/";
  const t = useTranslations("ui");
  const [mobile, setMobile] = useState(false);
  const [navigationOpen, setNavigationOpen] = useState(false);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 767px)");
    const sync = () => { setMobile(query.matches); if (!query.matches) setNavigationOpen(false); };
    sync();
    query.addEventListener("change", sync);
    const close = () => setNavigationOpen(false);
    window.addEventListener("hashchange", close);
    return () => { query.removeEventListener("change", sync); window.removeEventListener("hashchange", close); };
  }, []);
  useEffect(() => { setNavigationOpen(false); }, [pathname]);

  if (pathname === "/login" || pathname.startsWith("/browser-session/embed")) return <>{children}</>;
  const isSettingsSurface = SETTINGS_SURFACES.some((href) => pathname === href || pathname.startsWith(`${href}/`));
  const chatSurface = pathname === "/chat" || pathname.startsWith("/chat/");
  const fullBleed = pathname === "/" || chatSurface;
  const frame = isSettingsSurface
    ? "mx-auto w-full max-w-[1120px] px-4 pb-10 pt-1 lg:px-8"
    : "mx-auto w-full max-w-[1360px] px-4 pb-12 pt-2 lg:px-8";
  return (
    <AuthGate>
      <CommandPaletteProvider>
        <Dialog.Root open={navigationOpen} onOpenChange={setNavigationOpen}>
          <a href="#main-content" className="ui-skip-link" onClick={(event) => { event.preventDefault(); document.getElementById("main-content")?.focus(); }}>{t("skipToContent")}</a>
          <div className={`nerya-app-shell ${isSettingsSurface ? "nerya-settings-shell" : ""} flex min-h-0 overflow-hidden`}>
            {!mobile ? <div className="ui-desktop-navigation hidden shrink-0 md:block">{isSettingsSurface ? <SettingsSidebar /> : <CodexSidebar />}</div> : null}
            <main id="main-content" tabIndex={-1} className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
              {/* Chat owns a single task toolbar. Other surfaces keep the shell utilities. */}
              {!chatSurface ? <div className="relative z-30 flex h-12 shrink-0 items-center gap-2 px-3" data-testid="shell-utility-bar">
                <ShellNavigationTrigger />
                <div className="ml-auto"><ShellNotifications /></div>
              </div> : null}
              {fullBleed ? <div className="relative flex min-h-0 flex-1 flex-col">{children}</div> : (
                <div className="relative flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
                  <div className={frame}><PageTransition>{children}</PageTransition></div>
                </div>
              )}
            </main>
          </div>
          <Dialog.Portal>
            <Dialog.Overlay className="ui-modal-overlay ui-drawer-overlay" />
            <Dialog.Content className="ui-navigation-drawer" aria-describedby={undefined}>
              <div className="flex shrink-0 items-center justify-between border-b border-[color:var(--line)] px-4 py-2">
                <Dialog.Title className="text-sm font-semibold">{t("navigation")}</Dialog.Title>
                <Dialog.Close className="ui-icon-button" aria-label={t("closeNavigation")}><XIcon size={18} /></Dialog.Close>
              </div>
              <div className="min-h-0 flex-1" onClick={(event) => {
                if (event.target instanceof Element && event.target.closest("a, [data-navigation-action], [data-settings-section]")) setNavigationOpen(false);
              }}>
                {isSettingsSurface ? <SettingsSidebar /> : <CodexSidebar inDrawer />}
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </CommandPaletteProvider>
    </AuthGate>
  );
}
