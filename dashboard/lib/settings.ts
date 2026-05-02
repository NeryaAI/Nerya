"use client";

/**
 * Dashboard-level user preferences, persisted in localStorage.
 *
 * This is intentionally decoupled from the backend: these are purely visual
 * choices (which venue to pull candles from, which symbol to pin, what
 * interval to show, refresh cadence) and should survive reloads without
 * hitting the server.
 */

import { useSyncExternalStore } from "react";

export type KlineVenue =
  | "mock"
  | "binance"
  | "bybit"
  | "okx"
  | "hyperliquid";

export type KlineInterval = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

export type TimezonePreference =
  | "auto"
  | "utc+8"
  | "utc+0"
  | "utc-5"
  | "utc+9"
  | "utc-8";

export type LanguagePreference = "en" | "zh" | "ja";

export type MarketStreamPreference = "basic" | "standard" | "pro";

export type UiSettings = {
  kline: {
    venue: KlineVenue;
    symbol: string;   // plain pair, e.g. "BTCUSDT"
    interval: KlineInterval;
    count: number;    // number of candles to show
  };
  refreshSeconds: number; // dashboard auto-refresh cadence
  showVolume: boolean;
  chartType: "candlestick" | "line" | "area";
  compact: boolean;
  timezone: TimezonePreference;
  language: LanguagePreference;
  marketStream: MarketStreamPreference;
  darkMode: boolean;
};

export const DEFAULT_SETTINGS: UiSettings = {
  kline: {
    venue: "binance",
    symbol: "BTCUSDT",
    interval: "1h",
    count: 96,
  },
  refreshSeconds: 30,
  showVolume: true,
  chartType: "candlestick",
  compact: false,
  timezone: "auto",
  language: "en",
  marketStream: "standard",
  darkMode: true,
};

const KEY = "nerya.ui_settings.v1";
const EVT = "nerya:ui_settings_changed";

let cachedRaw: string | null | undefined;
let cachedSettings: UiSettings = DEFAULT_SETTINGS;

/** Merge helper that tolerates missing keys / extra fields in old blobs. */
function merge(stored: unknown): UiSettings {
  if (!stored || typeof stored !== "object") return DEFAULT_SETTINGS;
  const s = stored as Partial<UiSettings>;
  return {
    ...DEFAULT_SETTINGS,
    ...s,
    kline: { ...DEFAULT_SETTINGS.kline, ...(s.kline ?? {}) },
  };
}

export function loadSettings(): UiSettings {
  if (typeof window === "undefined") return DEFAULT_SETTINGS;
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw === cachedRaw) return cachedSettings;
    cachedRaw = raw;
    cachedSettings = raw ? merge(JSON.parse(raw)) : DEFAULT_SETTINGS;
    return cachedSettings;
  } catch {
    cachedSettings = DEFAULT_SETTINGS;
    return cachedSettings;
  }
}

export function saveSettings(next: UiSettings): void {
  if (typeof window === "undefined") return;
  try {
    const raw = JSON.stringify(next);
    cachedRaw = raw;
    cachedSettings = next;
    window.localStorage.setItem(KEY, raw);
    window.dispatchEvent(new CustomEvent(EVT));
  } catch {
    /* ignore quota / private-mode errors */
  }
}

export function patchSettings(patch: Partial<UiSettings>): UiSettings {
  const cur = loadSettings();
  const next: UiSettings = {
    ...cur,
    ...patch,
    kline: { ...cur.kline, ...(patch.kline ?? {}) },
  };
  saveSettings(next);
  return next;
}

/* ---------------------------------------------------------------- React hook */

function subscribe(cb: () => void) {
  if (typeof window === "undefined") return () => {};
  const handler = () => cb();
  window.addEventListener(EVT, handler);
  window.addEventListener("storage", handler);
  return () => {
    window.removeEventListener(EVT, handler);
    window.removeEventListener("storage", handler);
  };
}

function getSnapshot(): UiSettings {
  return loadSettings();
}

function getServerSnapshot(): UiSettings {
  return DEFAULT_SETTINGS;
}

export function useUiSettings(): [UiSettings, (p: Partial<UiSettings>) => void] {
  const value = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  return [value, patchSettings];
}
