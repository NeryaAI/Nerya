"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  AUTH_EVENT,
  getStoredAuthToken,
  isLocalDashboardHost,
  redirectToLogin,
} from "../lib/auth";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    function evaluate() {
      if (pathname === "/" || pathname === "/login" || isLocalDashboardHost()) {
        setReady(true);
        return;
      }
      if (getStoredAuthToken()) {
        setReady(true);
        return;
      }
      setReady(false);
      redirectToLogin();
    }

    evaluate();
    window.addEventListener(AUTH_EVENT, evaluate);
    window.addEventListener("storage", evaluate);
    return () => {
      window.removeEventListener(AUTH_EVENT, evaluate);
      window.removeEventListener("storage", evaluate);
    };
  }, [pathname]);

  if (!ready) {
    return null;
  }
  return <>{children}</>;
}
