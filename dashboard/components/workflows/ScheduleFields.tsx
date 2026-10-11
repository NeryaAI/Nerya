"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ApiError } from "../../lib/clientApi";
import { schedulePreset, validateSchedule, type ScheduleEditorValue, type SchedulePreview } from "../../lib/scheduleEditor";
import styles from "./WorkflowForms.module.css";

export function ScheduleFields({ value, onChange, disabled = false, allowOnce = true, showOverlap = true }: {
  value: ScheduleEditorValue; onChange: (patch: Partial<ScheduleEditorValue>) => void;
  disabled?: boolean; allowOnce?: boolean; showOverlap?: boolean;
}) {
  const t = useTranslations("workflowUpgrade"), locale = useLocale();
  const id = useId();
  const [preview, setPreview] = useState<SchedulePreview>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [needsFold, setNeedsFold] = useState(false);
  const request = useRef<AbortController>();
  const key = JSON.stringify(value);
  useEffect(() => { request.current?.abort(); setPreview(undefined); setError(""); setNeedsFold(false); setBusy(false); return () => request.current?.abort(); }, [key]);
  const preset = schedulePreset(value);
  const [minute = "0", hour = "9", , , day = "1"] = value.cron.trim().split(/\s+/);
  const clock = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
  function changePreset(next: string) {
    if (next === "interval" || next === "once") onChange({ cadence: next });
    else onChange({ cadence: "cron", cron: next === "cron" ? value.cron || "0 9 * * *" : `0 9 * * ${next === "weekdays" ? "1-5" : next === "weekly" ? "1" : "*"}` });
  }
  async function inspect() {
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setBusy(true); setError("");
    try { const result = await validateSchedule(value, controller.signal); if (!controller.signal.aborted) setPreview(result); }
    catch (reason) {
      if (!controller.signal.aborted) {
        const code = reason instanceof ApiError && typeof reason.payload === "object" && reason.payload !== null ? String((reason.payload as Record<string, unknown>).error || "") : "";
        setNeedsFold(code === "ambiguous_local_time");
        setError(code && t.has(code) ? t(code) : String(reason));
      }
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <section className={styles.section} data-testid="schedule-fields">
    <h3>{t("schedule")}</h3>
    <div className={styles.grid}>
      <label className={styles.field}><span>{t("schedule")}</span><select className="input-dark" value={preset} disabled={disabled} onChange={event => changePreset(event.target.value)}>{[...(allowOnce ? ["once"] : []), "interval", "daily", "weekdays", "weekly", "cron"].map(choice => <option key={choice} value={choice}>{t(choice)}</option>)}</select></label>
      {preset === "once" ? <label className={styles.field}><span>{t("wallTime")}</span><input className="input-dark" type="datetime-local" value={value.runAt} disabled={disabled} onChange={event => onChange({ runAt: event.target.value, fold: undefined })} /></label>
        : preset === "interval" ? <label className={styles.field}><span>{t("everyMinutes")}</span><input className="input-dark" type="number" min={1 / 60} step="any" value={value.everySeconds ? Number(value.everySeconds) / 60 : ""} disabled={disabled} onChange={event => onChange({ everySeconds: event.target.value ? String(Math.round(Number(event.target.value) * 60)) : "" })} /></label>
        : preset === "cron" ? <label className={styles.field}><span>{t("cronExpression")}</span><input className="input-dark font-mono" value={value.cron} maxLength={128} disabled={disabled} onChange={event => onChange({ cron: event.target.value })} /></label>
        : <label className={styles.field}><span>{t("timeOfDay")}</span><input className="input-dark" type="time" value={clock} disabled={disabled} onChange={event => { const [h, m] = event.target.value.split(":"); if (h && m) onChange({ cron: `${Number(m)} ${Number(h)} * * ${preset === "weekdays" ? "1-5" : preset === "weekly" ? day : "*"}` }); }} /></label>}
      {preset === "weekly" && <label className={styles.field}><span>{t("weekday")}</span><select className="input-dark" value={day} disabled={disabled} onChange={event => onChange({ cron: `${minute} ${hour} * * ${event.target.value}` })}>{Array.from({ length: 7 }, (_, index) => <option key={index} value={index}>{new Intl.DateTimeFormat(locale, { weekday: "long", timeZone: "UTC" }).format(new Date(Date.UTC(2026, 0, 4 + index)))}</option>)}</select></label>}
      <label className={styles.field}><span>{t("timezone")}</span><input className="input-dark" list={`${id}-zones`} value={value.timezone} disabled={disabled} placeholder="Asia/Shanghai" onChange={event => onChange({ timezone: event.target.value, fold: undefined })} /><datalist id={`${id}-zones`}>{["UTC", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Seoul", "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin"].map(zone => <option key={zone} value={zone} />)}</datalist></label>
      {showOverlap && <label className={styles.field}><span>{t("overlap")}</span><select className="input-dark" value={value.overlapPolicy || "skip"} disabled={disabled} onChange={event => onChange({ overlapPolicy: event.target.value as ScheduleEditorValue["overlapPolicy"] })}>{["skip", "coalesce", "queue"].map(policy => <option key={policy} value={policy}>{t(policy)}</option>)}</select></label>}
    </div>
    {preset === "once" && <details className="mt-3 text-xs text-[color:var(--text-muted)]" open={needsFold || value.fold !== undefined}><summary className="cursor-pointer">{t("timeFold")}</summary><label className={`${styles.field} mt-3`}><span>{t("timeFold")}</span><select className="input-dark" value={value.fold ?? ""} disabled={disabled} onChange={event => onChange({ fold: event.target.value === "" ? undefined : Number(event.target.value) as 0 | 1 })}><option value="">{t("defaultFold")}</option><option value="0">{t("earlierFold")}</option><option value="1">{t("laterFold")}</option></select></label></details>}
    <div className={styles.previewActions}><button type="button" className="btn-ghost" disabled={disabled || busy} onClick={() => void inspect()}>{busy ? t("loading") : t("preview")}</button><p>{t("previewHint")}</p></div>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    {preview && <div className={styles.preview} role="status"><strong>{t("previewTimes")}</strong><ol>{preview.occurrences.map(occurrence => <li key={occurrence.utc}><time dateTime={occurrence.utc}>{new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: value.timezone || "UTC" }).format(new Date(occurrence.utc))} · {value.timezone || "UTC"}</time><small>{t("localTime", { time: new Date(occurrence.utc).toLocaleString(locale) })}</small></li>)}</ol>{!preview.occurrences.length && <p>{t("noFutureTimes")}</p>}{preview.interval_unanchored && <p>{t("intervalUnanchored")}</p>}</div>}
  </section>;
}
