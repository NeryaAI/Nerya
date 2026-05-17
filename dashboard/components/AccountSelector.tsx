"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi, type AccountSummary } from "../lib/clientApi";
import { useCurrentAccountId } from "../lib/currentAccount";

/**
 * Top-bar account selector.
 *
 * Replaces the static Paper/Live indicator with a real chooser that
 * spans every account configured in ``accounts.yml``. The chosen id
 * is mirrored into localStorage via ``useCurrentAccountId`` so the
 * Home page KPIs, the strategy bind UI, and any future per-account
 * surface stay in sync without prop drilling.
 *
 * Modes are colour-coded inline so a single glance tells the
 * operator whether they're focused on paper, shadow, canary or live
 * — the old binary toggle could only ever say "Paper" or "Live".
 */

function modeColor(mode: string): string {
  switch (mode) {
    case "live":
      return "bg-rose-500/15 text-rose-200 border-rose-500/30";
    case "canary":
      return "bg-amber-400/15 text-amber-200 border-amber-400/30";
    case "shadow":
      return "bg-brand-500/15 text-brand-200 border-brand-500/30";
    case "paper":
    default:
      return "bg-emerald-500/15 text-emerald-200 border-emerald-500/30";
  }
}

function statusColor(status: string): string {
  switch (status) {
    case "active":
      return "bg-emerald-500";
    case "read_only":
      return "bg-amber-400";
    case "quarantined":
    case "disabled":
      return "bg-rose-500";
    default:
      return "bg-ink-500";
  }
}

export function AccountSelector() {
  const t = useTranslations("accountSelector");
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [open, setOpen] = useState(false);
  const [currentId, setCurrentId] = useCurrentAccountId();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await clientApi.accountsList();
        if (cancelled) return;
        const sorted = [...(res.accounts ?? [])].sort((a, b) =>
          a.profile.id.localeCompare(b.profile.id),
        );
        setAccounts(sorted);
        // Default to first active account if no selection yet.
        if (!currentId && sorted.length > 0) {
          const firstActive =
            sorted.find((a) => a.profile.status === "active") ?? sorted[0];
          setCurrentId(firstActive.profile.id);
        }
      } catch {
        /* unreachable backend handled elsewhere */
      }
    }
    void load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Close menu on outside click.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      const target = e.target as HTMLElement;
      if (!target.closest("[data-account-selector]")) setOpen(false);
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  if (accounts.length === 0) {
    return (
      <Link
        href="/accounts"
        className="flex max-w-[42vw] items-center gap-2 truncate rounded-lg border border-amber-400/30 bg-amber-400/5 px-2 py-1.5 text-xs text-amber-200 hover:bg-amber-400/10 sm:max-w-none sm:px-3"
        title={t("noAccountsTitle")}
      >
        {t("addAccount")}
      </Link>
    );
  }

  const selected =
    accounts.find((a) => a.profile.id === currentId) ?? accounts[0];

  return (
    <div className="relative min-w-0" data-account-selector>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex max-w-[42vw] items-center gap-1.5 rounded-lg border px-2 py-1.5 text-xs sm:max-w-none sm:gap-2 sm:px-3 ${modeColor(
          selected.profile.mode,
        )} hover:brightness-110`}
        title={t("focusedOn", { mode: selected.profile.mode, venue: selected.profile.venue })}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full ${statusColor(
            selected.profile.status,
          )}`}
        />
        <span className="max-w-[86px] truncate font-mono text-[12px] sm:max-w-[160px]">
          {selected.profile.id}
        </span>
        <span className="hidden text-[12px] opacity-70 sm:inline">
          {selected.profile.mode}
        </span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          className="opacity-60"
          aria-hidden="true"
        >
          <path d="M2 3l3 4 3-4" stroke="currentColor" fill="none" />
        </svg>
      </button>
      {open ? (
        <div
          className="absolute right-0 z-50 mt-2 w-[calc(100vw-1rem)] max-w-[20rem] rounded-xl border border-brand-500/20 bg-[#0c0d1d] shadow-2xl sm:w-80"
          role="menu"
        >
          <div className="px-3 py-2 border-b border-brand-500/10 text-[12px] text-ink-400 flex items-center justify-between">
            <span>{t("focusedAccount")}</span>
            <Link
              href="/accounts"
              className="text-brand-300 hover:text-brand-200"
              onClick={() => setOpen(false)}
            >
              {t("manage")}
            </Link>
          </div>
          <ul className="embedded-list-scroll py-1">
            {accounts.map((acc) => {
              const p = acc.profile;
              const active = p.id === selected.profile.id;
              return (
                <li key={p.id}>
                  <button
                    onClick={() => {
                      setCurrentId(p.id);
                      setOpen(false);
                    }}
                    className={`w-full text-left px-3 py-2 hover:bg-white/[0.04] flex items-center gap-2 ${
                      active ? "bg-brand-500/10" : ""
                    }`}
                    title={`${p.venue} · ${p.kind} · ${p.status}`}
                  >
                    <span
                      className={`w-1.5 h-1.5 rounded-full ${statusColor(
                        p.status,
                      )}`}
                    />
                    <span className="font-mono text-xs text-ink-100 flex-1 truncate">
                      {p.id}
                    </span>
                    <span
                      className={`text-[11px] px-1.5 py-0.5 rounded border ${modeColor(
                        p.mode,
                      )}`}
                    >
                      {p.mode}
                    </span>
                    <span className="text-[11px] font-mono text-ink-400 w-14 text-right">
                      {p.base_currency || "USDT"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="px-3 py-2 border-t border-brand-500/10 flex items-center justify-between text-[11px]">
            <button
              onClick={() => {
                setCurrentId(null);
                setOpen(false);
              }}
              className="text-ink-400 hover:text-ink-200"
            >
              {t("clearFocus")}
            </button>
            <Link
              href={`/accounts/${encodeURIComponent(selected.profile.id)}`}
              className="text-brand-200 hover:text-brand-100"
              onClick={() => setOpen(false)}
            >
              {t("openDriver")}
            </Link>
          </div>
        </div>
      ) : null}
    </div>
  );
}
