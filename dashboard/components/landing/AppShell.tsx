"use client";

import { usePathname } from "next/navigation";
import { Sidebar } from "../Sidebar";
import { TopHeader } from "../TopHeader";
import { PageTransition } from "../PageTransition";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLanding = pathname === "/";

  if (isLanding) {
    return <>{children}</>;
  }

  return (
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
  );
}
