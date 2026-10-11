import { callApi } from "./clientApi";

export type ScheduleEditorValue = {
  cadence: "cron" | "interval" | "once";
  cron: string;
  everySeconds: string;
  timezone: string;
  runAt: string;
  runAtInstant?: string;
  fold?: 0 | 1;
  anchorAt?: number;
  startsAt?: string | null;
  endsAt?: string | null;
  overlapPolicy?: "skip" | "coalesce" | "queue";
};
export type SchedulePreview = {
  ok: boolean; error?: string; read_only: boolean;
  schedule: { cron: string | null; every_seconds: number | null; run_at: string | null; timezone: string };
  occurrences: Array<{ utc: string; local: string }>;
  interval_unanchored: boolean;
};

export function wallTimeInZone(instant: string, zone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-GB-u-nu-latn", { timeZone: zone || "UTC", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(instant));
    const field = (type: string) => parts.find(part => part.type === type)?.value || "";
    return `${field("year")}-${field("month")}-${field("day")}T${field("hour")}:${field("minute")}`;
  } catch { return ""; }
}

export function schedulePreviewPayload(value: ScheduleEditorValue) {
  return {
    timezone: value.timezone || "UTC",
    cron: value.cadence === "cron" ? value.cron.trim() : null,
    every_seconds: value.cadence === "interval" ? Number(value.everySeconds) : null,
    run_at: value.cadence === "once" ? value.runAtInstant || null : null,
    ...(value.cadence === "once" ? { wall_time: value.runAt, ...(value.fold !== undefined ? { fold: value.fold } : {}) } : {}),
    ...(value.anchorAt !== undefined ? { anchor_at: value.anchorAt } : {}),
    starts_at: value.startsAt || null, ends_at: value.endsAt || null,
    overlap_policy: value.overlapPolicy || "skip",
  };
}

export async function validateSchedule(value: ScheduleEditorValue, signal?: AbortSignal): Promise<SchedulePreview> {
  const result = await callApi<SchedulePreview>("/triggers/schedules/preview", { method: "POST", body: { schedule: schedulePreviewPayload(value) }, signal });
  if (!result.ok || !result.read_only || !Array.isArray(result.occurrences)) throw new Error(result.error || "Schedule preview unavailable");
  return result;
}

export function schedulePreset(value: ScheduleEditorValue): string {
  if (value.cadence !== "cron") return value.cadence;
  const [minute, hour, day, month, weekday, extra] = value.cron.trim().split(/\s+/);
  if (extra || !/^\d+$/.test(minute || "") || !/^\d+$/.test(hour || "") || day !== "*" || month !== "*") return "cron";
  return weekday === "*" ? "daily" : weekday === "1-5" ? "weekdays" : /^[0-6]$/.test(weekday || "") ? "weekly" : "cron";
}
