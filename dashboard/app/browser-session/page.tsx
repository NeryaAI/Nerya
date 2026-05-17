"use client";

/**
 * Legacy /browser-session route. The standalone interactive browser
 * driver has been merged into /browsers as a sibling "Session" sub-tab
 * (alongside the engine inventory) so the operator has one workspace
 * for everything browser-related. This page is now a thin redirect
 * that preserves the legacy deep-link.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function BrowserSessionLegacyRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/browsers?tab=session");
  }, [router]);
  return (
    <div className="px-6 py-12 text-[12px] text-ink-400">
      Redirecting to /browsers?tab=session …
    </div>
  );
}
