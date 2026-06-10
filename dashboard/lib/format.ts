import { loadSettings, type TimezonePreference } from "./settings";

export function pretty(value: unknown, indent = 2): string {
  try {
    return JSON.stringify(value, null, indent);
  } catch {
    return String(value);
  }
}

export function truncate(text: string, n = 120): string {
  if (!text) return "";
  return text.length > n ? text.slice(0, n - 1) + "\u2026" : text;
}

/**
 * Human-readable schedule cadence. Raw cron strings (`*\/5 * * * *`)
 * mean nothing to most users, so the common shapes get a translation
 * key + params the caller feeds into next-intl. Anything fancier
 * (day-of-week, lists, ranges) returns null and the caller falls back
 * to the raw expression.
 */
export type CadenceDescription = {
  key:
    | "everyMinute"
    | "everyNMinutes"
    | "everyNHours"
    | "everyNDays"
    | "everyNSeconds"
    | "dailyAt";
  params?: Record<string, string | number>;
};

export function describeCron(cron: string): CadenceDescription | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [min, hour, dom, mon, dow] = parts;
  if (dom !== "*" || mon !== "*" || dow !== "*") return null;
  if (min === "*" && hour === "*") return { key: "everyMinute" };
  const stepMin = /^\*\/(\d+)$/.exec(min);
  if (stepMin && hour === "*") {
    return { key: "everyNMinutes", params: { n: Number(stepMin[1]) } };
  }
  const stepHour = /^\*\/(\d+)$/.exec(hour);
  if (/^\d+$/.test(min) && stepHour) {
    return { key: "everyNHours", params: { n: Number(stepHour[1]) } };
  }
  if (/^\d+$/.test(min) && /^\d+$/.test(hour)) {
    const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
    return { key: "dailyAt", params: { time } };
  }
  return null;
}

export function describeIntervalSeconds(
  seconds: number,
): CadenceDescription | null {
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds % 86400 === 0) return { key: "everyNDays", params: { n: seconds / 86400 } };
  if (seconds % 3600 === 0) return { key: "everyNHours", params: { n: seconds / 3600 } };
  if (seconds % 60 === 0) return { key: "everyNMinutes", params: { n: seconds / 60 } };
  return { key: "everyNSeconds", params: { n: seconds } };
}

const TZ_OFFSET_MINUTES: Record<Exclude<TimezonePreference, "auto">, number> = {
  "utc+0": 0,
  "utc+8": 8 * 60,
  "utc+9": 9 * 60,
  "utc-5": -5 * 60,
  "utc-8": -8 * 60,
};

const TZ_LABEL: Record<Exclude<TimezonePreference, "auto">, string> = {
  "utc+0": "UTC",
  "utc+8": "UTC+8",
  "utc+9": "UTC+9",
  "utc-5": "UTC-5",
  "utc-8": "UTC-8",
};

function parseDate(ts: string | number): Date {
  if (typeof ts !== "number") return new Date(ts);
  // Heuristic: treat values larger than ~year-2001-in-seconds as already
  // being in milliseconds. Python-side envelopes usually send ISO strings
  // or epoch seconds; the chat store uses `Date.now()` milliseconds.
  return new Date(ts > 1e12 ? ts : ts * 1000);
}

function currentTimezone(): TimezonePreference {
  try {
    return loadSettings().timezone;
  } catch {
    return "auto";
  }
}

/**
 * Format a timestamp using the operator's dashboard-only timezone
 * preference (see `UiSettings.timezone`).
 *
 * - "auto"  → browser locale, 24h: "YYYY-MM-DD HH:mm:ss (local)"
 * - "utc+0" → "YYYY-MM-DD HH:mm:ss UTC"
 * - "utc+8" etc. → same shape, shifted by the preference offset
 */
export function formatTs(
  ts: string | number | undefined,
  override?: TimezonePreference,
): string {
  if (!ts) return "–";
  const d = parseDate(ts);
  if (Number.isNaN(d.getTime())) return String(ts);

  const tz = override ?? currentTimezone();
  if (tz === "auto") {
    const pad = (n: number) => String(n).padStart(2, "0");
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())} local`
    );
  }
  const offsetMinutes = TZ_OFFSET_MINUTES[tz] ?? 0;
  const shifted = new Date(d.getTime() + offsetMinutes * 60 * 1000);
  const iso = shifted.toISOString().replace("T", " ").slice(0, 19);
  return `${iso} ${TZ_LABEL[tz] ?? "UTC"}`;
}

/** Same as `formatTs` but drops the timezone suffix. Useful inside tight cells. */
export function formatTsShort(
  ts: string | number | undefined,
  override?: TimezonePreference,
): string {
  const full = formatTs(ts, override);
  return full.replace(/\s+(UTC[+-]?\d*|local)$/i, "");
}

/** Render just HH:MM:SS in the operator's preferred timezone. */
export function formatTime(
  ts: string | number | undefined,
  override?: TimezonePreference,
): string {
  if (!ts) return "–";
  const d = parseDate(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const tz = override ?? currentTimezone();
  if (tz === "auto") {
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  const offsetMinutes = TZ_OFFSET_MINUTES[tz] ?? 0;
  const shifted = new Date(d.getTime() + offsetMinutes * 60 * 1000);
  return shifted.toISOString().slice(11, 19);
}

/** Label the currently-active timezone preference for display. */
export function timezoneLabel(pref: TimezonePreference = currentTimezone()): string {
  if (pref === "auto") return "local";
  return TZ_LABEL[pref] ?? "UTC";
}
