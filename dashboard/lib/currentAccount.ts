"use client";

/**
 * Currently-focused account.
 *
 * Persists the operator's "active account" choice so the top-level
 * chrome (TopHeader account selector), the Home page KPIs, and any
 * other multi-account surface can stay in sync without prop-drilling.
 *
 * The shape mirrors :func:`useUiSettings` — localStorage backed, with
 * a custom event for cross-tab fan-out so a dropdown change in the
 * header re-renders the home page.
 */

import { useSyncExternalStore } from "react";

const KEY = "nerya.currentAccountId.v1";
const EVT = "nerya:currentAccountId";

function readId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw && raw.trim() ? raw : null;
  } catch {
    return null;
  }
}

function writeId(value: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (value && value.trim()) {
      window.localStorage.setItem(KEY, value.trim());
    } else {
      window.localStorage.removeItem(KEY);
    }
    window.dispatchEvent(new CustomEvent(EVT));
  } catch {
    /* swallow quota / private-mode errors */
  }
}

function subscribe(cb: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const handler = () => cb();
  window.addEventListener(EVT, handler);
  window.addEventListener("storage", handler);
  return () => {
    window.removeEventListener(EVT, handler);
    window.removeEventListener("storage", handler);
  };
}

function getSnapshot(): string | null {
  return readId();
}

function getServerSnapshot(): string | null {
  return null;
}

export function useCurrentAccountId(): [
  string | null,
  (id: string | null) => void,
] {
  const value = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return [value, writeId];
}

export function setCurrentAccountId(id: string | null): void {
  writeId(id);
}

/* -------------------------------------------------------------- currency utils */

/**
 * Per-account currency formatter.
 *
 * Each :class:`AccountProfile` carries a ``base_currency`` (USDT by
 * default, but the YAML accepts any ISO-4217-ish ticker — CNY for
 * A-share, JPY for Japan venues, USDC for on-chain etc). The
 * dashboard uses this helper instead of a hardcoded ``$`` so a CNY
 * account renders ``¥``, a USDT/USDC account renders the explicit
 * ticker, and so on.
 */

const SYMBOL_MAP: Record<string, string> = {
  USD: "$",
  USDT: "$",
  USDC: "$",
  BUSD: "$",
  CNY: "¥",
  RMB: "¥",
  JPY: "¥",
  EUR: "€",
  GBP: "£",
  HKD: "HK$",
  KRW: "₩",
  INR: "₹",
  BTC: "₿",
};

export function currencySymbol(code: string | undefined | null): string {
  if (!code) return "$";
  const up = String(code).toUpperCase();
  return SYMBOL_MAP[up] ?? `${up} `;
}

export function formatBalance(
  value: unknown,
  currency: string | undefined | null = "USDT",
  digits = 2,
): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "–";
  const symbol = currencySymbol(currency);
  const formatted = n.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
  // For tickers without a glyph we already render "USDT " etc as the
  // symbol so the result reads "USDT 1,234.56".
  return `${symbol}${formatted}`;
}
