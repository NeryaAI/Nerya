"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { clientApi, type AccountSummary } from "../lib/clientApi";
import { useCurrentAccountId } from "../lib/currentAccount";

/**
 * Top-bar account selector (04-29 §11 P9).
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
        className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-lg border border-amber-400/30 bg-amber-400/5 text-xs text-amber-200 hover:bg-amber-400/10"
        title="No accounts configured yet"
      >
        + Add account
      </Link>
    );
  }

  const selected =
    accounts.find((a) => a.profile.id === currentId) ?? accounts[0];

  return (
    <div className="relative" data-account-selector>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs ${modeColor(
          selected.profile.mode,
        )} hover:brightness-110`}
        title={`Focused account (${selected.profile.mode} on ${selected.profile.venue})`}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full ${statusColor(
            selected.profile.status,
          )}`}
        />
        <span className="font-mono text-[12px]">{selected.profile.id}</span>
        <span className="text-[10px] uppercase tracking-widest opacity-70">
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
          className="absolute right-0 mt-2 w-80 rounded-xl border border-white/10 bg-[#0c0d1d] shadow-2xl z-50"
          role="menu"
        >
          <div className="px-3 py-2 border-b border-white/5 text-[11px] uppercase tracking-widest text-ink-400 flex items-center justify-between">
            <span>Focused account</span>
            <Link
              href="/accounts"
              className="text-brand-200 hover:text-brand-100 normal-case tracking-normal"
              onClick={() => setOpen(false)}
            >
              Manage →
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
                      className={`text-[10px] px-1.5 py-0.5 rounded border ${modeColor(
                        p.mode,
                      )}`}
                    >
                      {p.mode}
                    </span>
                    <span className="text-[10px] font-mono text-ink-400 w-14 text-right">
                      {p.base_currency || "USDT"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="px-3 py-2 border-t border-white/5 flex items-center justify-between text-[11px]">
            <button
              onClick={() => {
                setCurrentId(null);
                setOpen(false);
              }}
              className="text-ink-400 hover:text-ink-200"
            >
              Clear focus
            </button>
            <Link
              href={`/accounts/${encodeURIComponent(selected.profile.id)}`}
              className="text-brand-200 hover:text-brand-100"
              onClick={() => setOpen(false)}
            >
              Open driver →
            </Link>
          </div>
        </div>
      ) : null}
    </div>
  );
}
