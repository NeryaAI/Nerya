"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { NeryaLogo } from "../../components/NeryaLogo";
import { ErrorBanner, Pill } from "../../components/Page";
import { clientApi, type AuthStatus } from "../../lib/clientApi";
import { getStoredAuthToken, isLocalDashboardHost, setStoredAuthToken } from "../../lib/auth";

export default function LoginPage() {
  const t = useTranslations("login");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [nextPath, setNextPath] = useState("/dashboard");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const next = params.get("next");
    if (next && next.startsWith("/")) setNextPath(next);
    void clientApi.authStatus().then(setStatus).catch((e) => {
      setError(e instanceof Error ? e.message : String(e));
    });
  }, []);

  const localHost = useMemo(() => {
    if (typeof window === "undefined") return false;
    return isLocalDashboardHost(window.location.hostname);
  }, []);

  useEffect(() => {
    if (!localHost && getStoredAuthToken()) {
      window.location.replace(nextPath);
    }
  }, [localHost, nextPath]);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await clientApi.authLogin({ password });
      if (!res.ok || !res.token) {
        throw new Error(res.detail || res.error || "login_failed");
      }
      setStoredAuthToken(res.token, res.expires_at);
      window.location.replace(nextPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-screen bg-ink-950 text-ink-100">
      <div className="mx-auto flex min-h-screen w-full max-w-6xl items-center justify-center px-5 py-10">
        <section className="grid w-full gap-6 lg:grid-cols-[1fr_420px] lg:items-center">
          <div className="hidden lg:block">
            <div className="flex items-center gap-4">
              <div className="relative h-16 w-16 overflow-hidden rounded-2xl bg-black/40 ring-1 ring-brand-500/40">
                <NeryaLogo size={64} />
              </div>
              <div>
                <div className="text-[13px] font-medium text-fluid-300">
                  Nerya
                </div>
                <h1 className="mt-2 text-[34px] font-medium tracking-tight text-white">
                  {t("headline")}
                </h1>
              </div>
            </div>
            <p className="mt-5 max-w-xl text-sm leading-6 text-ink-400">
              {t("description")}
            </p>
          </div>

          <form
            onSubmit={submit}
            className="card p-5"
          >
            <div className="mb-5 flex items-start justify-between gap-3">
              <div>
                <h2 className="text-xl font-semibold text-white">{t("title")}</h2>
                <p className="mt-1 text-[12px] leading-5 text-ink-400">
                  {t("subtitle")}
                </p>
              </div>
              <Pill tone={status?.password_configured ? "ok" : "warn"}>
                {status?.password_configured ? t("configured") : t("notConfigured")}
              </Pill>
            </div>

            {error ? <ErrorBanner error={error} /> : null}

            {!status?.password_configured ? (
              <div className="mt-4 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-[12px] leading-5 text-amber-100">
                {t("setupRequired")}
              </div>
            ) : null}

            <label className="mt-4 block text-[12px] text-ink-300">
              {t("password")}
              <input
                className="input-dark mt-1"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                disabled={!status?.password_configured || busy}
              />
            </label>

            <button
              type="submit"
              className="btn btn-primary mt-5 w-full justify-center"
              disabled={!status?.password_configured || !password || busy}
            >
              {busy ? t("signingIn") : t("signIn")}
            </button>

            {localHost ? (
              <p className="mt-4 text-[11px] leading-5 text-ink-500">
                {t("localNote")}
              </p>
            ) : null}
          </form>
        </section>
      </div>
    </main>
  );
}
