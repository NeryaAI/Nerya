import type { TriggerDeliveryTarget, TriggerSchedule } from "./clientApi";
import { wallTimeInZone, type ScheduleEditorValue } from "./scheduleEditor";

export type ScheduleKind = "agent" | "script" | "trigger";
export type CadenceKind = "cron" | "interval" | 'once';
export type DeliveryKind = "none" | "gateway" | "messages" | "webhook";
export type ScheduleFilter = "all" | "agent" | "script" | "trigger" | "active" | "paused";
export type Translate = (key: string, values?: Record<string, string | number>) => string;

export type ScheduleDraft = ScheduleEditorValue & {
  title: string;
  originalSourceRequest: string;
  usePromptOverride: boolean;
  overrideAcknowledged: boolean;
  preserved: Partial<TriggerSchedule>;
  originalDeliveryTargets: TriggerDeliveryTarget[];
  deliveryEdited: boolean;
  id: string;
  kind: string;
  enabled: boolean;
  cadence: CadenceKind;
  cron: string;
  everySeconds: string;
  timezone: string;
  sessionKind: ScheduleKind;
  sessionMode: "ephemeral" | "reuse" | "fanout";
  sessionId: string;
  sessionIds: string;
  attachedSkills: string;
  sourceRequest: string;
  generatedPrompt: string;
  scriptId: string;
  scriptArgsJson: string;
  triggerPayloadJson: string;
  target: string;
  deliveryKind: DeliveryKind;
  deliveryPlatform: string;
  deliveryChannel: string;
  deliveryUrl: string;
  /** Targets beyond the first, kept verbatim as a JSON array so an
      edit-save cycle never silently drops them (P0 data-loss fix). */
  extraDeliveryJson: string;
  ttlSeconds: string;
  runAt:string;overlapPolicy:'skip'|'coalesce'|'queue';execution:Record<string,unknown>;
};

export const EMPTY_OBJECT = "{}";

export function blankDraft(): ScheduleDraft {
  return {
    title: "", originalSourceRequest: "", usePromptOverride: false, overrideAcknowledged: true,
    preserved: {}, originalDeliveryTargets: [], deliveryEdited: false,
    id: "",
    kind: "agent.task",
    enabled: false,
    cadence: "cron",
    cron: "0 9 * * *",
    everySeconds: "3600",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    anchorAt: Date.now() / 1000,
    sessionKind: "agent",
    sessionMode: "reuse",
    sessionId: "",
    sessionIds: "",
    attachedSkills: "",
    sourceRequest: "",
    generatedPrompt: "",
    scriptId: "",
    scriptArgsJson: EMPTY_OBJECT,
    triggerPayloadJson: EMPTY_OBJECT,
    target: "",
    deliveryKind: "none",
    deliveryPlatform: "telegram",
    deliveryChannel: "telegram",
    deliveryUrl: "",
    extraDeliveryJson: "",
    ttlSeconds: "",
    runAt:'',overlapPolicy:'skip',execution:{},
  };
}

export function scheduleKind(schedule: TriggerSchedule): ScheduleKind {
  const raw = String(schedule.session_kind || "").toLowerCase();
  if (raw === "agent" || raw === "script") return raw;
  return "trigger";
}

export function isPaused(schedule: TriggerSchedule): boolean {
  return schedule.enabled === false || schedule.paused === true;
}

export function parseJsonObject(text: string, label: string): Record<string, unknown> {
  const raw = text.trim();
  if (!raw) return {};
  const value = JSON.parse(raw) as unknown;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value as Record<string, unknown>;
}

export function parseCsv(text: string): string[] {
  return text
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function titleFor(schedule: TriggerSchedule): string {
  const payload = schedule.payload || {};
  const title = payload.title || payload.source_request || schedule.description || schedule.title;
  return String(title || schedule.id);
}

export function cadenceFor(schedule: TriggerSchedule, locale?: string): string {
  if (schedule.run_at) {
    try { return new Date(schedule.run_at).toLocaleString(locale, { timeZone: schedule.timezone || "UTC" }); }
    catch { return schedule.run_at; }
  }
  if (schedule.cron) return schedule.cron;
  if (schedule.every_seconds != null) return `${schedule.every_seconds}s`;
  if (schedule.interval != null) return String(schedule.interval);
  return "-";
}

/**
 * Translated human label for a schedule's cadence ("Every 5 min")
 * with the raw cron / interval as fallback for shapes the
 * describer doesn't cover.
 */
export function deliverySummary(schedule: TriggerSchedule): string {
  const targets = schedule.delivery_targets || [];
  if (!targets.length) return "-";
  return targets
    .map((target) => {
      const kind = String(target.kind || "?");
      const platform = String(target.platform || target.channel || target.url || "");
      return platform ? `${kind}:${platform}` : kind;
    })
    .join(", ");
}

export function statusTone(schedule: TriggerSchedule): "ok" | "brand" | "warn" {
  // Participation in a schedule is not proof of successful execution.
  return schedule.paused_reason ? "warn" : "brand";
}

/** Single source of truth for status colors: list dots and detail pills
 * both derive from statusTone() so one state never renders two colors.
 * Token classes only (bg-ok / bg-warn), no raw emerald/amber. */
export const STATUS_DOT_CLASS: Record<"ok" | "brand" | "warn", string> = {
  ok: "bg-ok",
  brand: "bg-brand-300",
  warn: "bg-warn",
};

export function buildDraftFromSchedule(schedule: TriggerSchedule): ScheduleDraft {
  const kind = scheduleKind(schedule);
  const payload = schedule.payload || {};
  const explicitOverride = payload.prompt_source === "operator_override";
  const allTargets = schedule.delivery_targets || [];
  const firstDelivery = allTargets[0] || null;
  const deliveryKind = firstDelivery
    ? String(firstDelivery.kind || "none") === "webhook"
      ? "webhook"
      : String(firstDelivery.kind || "none") === "messages"
      ? "messages"
      : "gateway"
    : "none";
  const scriptId =
    String(payload.script_id || "") ||
    (String(schedule.target || "").startsWith("script:")
      ? String(schedule.target).slice("script:".length)
      : "");
  return {
    ...blankDraft(),
    title: String(payload.title || schedule.title || ""),
    originalSourceRequest: String(payload.source_request || ""),
    preserved: { ...schedule },
    originalDeliveryTargets: allTargets.map(target => ({ ...target })),
    anchorAt: schedule.anchor_at,
    startsAt: schedule.starts_at, endsAt: schedule.ends_at,
    id: schedule.id,
    kind: schedule.kind || `${kind}.task`,
    enabled: !isPaused(schedule),
    cadence: schedule.run_at?'once':schedule.cron ? "cron" : "interval",
    runAt: schedule.run_at ? wallTimeInZone(schedule.run_at, schedule.timezone || "UTC") : "",
    runAtInstant: schedule.run_at || undefined,
    overlapPolicy:schedule.overlap_policy||'skip',execution:schedule.execution||{},
    cron: schedule.cron || "0 11 * * *",
    everySeconds: String(schedule.every_seconds ?? 3600),
    timezone: schedule.timezone || "UTC",
    sessionKind: kind,
    sessionMode: schedule.session_mode || "reuse",
    sessionId: schedule.session_id || "",
    sessionIds: (schedule.session_ids || []).join(", "),
    attachedSkills: (schedule.attached_skills || []).join(", "),
    // The primary field shows the instructions that actually execute. Older
    // source requests stay available as provenance, not a competing editor.
    sourceRequest: String(explicitOverride ? payload.source_request || payload.prompt || "" : payload.prompt || payload.source_request || ""),
    generatedPrompt: explicitOverride ? String(payload.prompt || "") : "",
    usePromptOverride: explicitOverride,
    scriptId,
    scriptArgsJson: JSON.stringify(payload.args || {}, null, 2),
    triggerPayloadJson: JSON.stringify(
      kind === "trigger" ? payload : withoutTaskPayloadFields(payload),
      null,
      2,
    ),
    target:
      kind === "script" && scriptId
        ? `script:${scriptId}`
        : schedule.target || (kind === "agent" ? "agent" : "main"),
    deliveryKind,
    deliveryPlatform: String(firstDelivery?.platform || firstDelivery?.channel || "telegram"),
    deliveryChannel: String(firstDelivery?.channel || firstDelivery?.platform || "telegram"),
    deliveryUrl: String(firstDelivery?.url || ""),
    extraDeliveryJson:
      allTargets.length > 1 ? JSON.stringify(allTargets.slice(1), null, 2) : "",
    ttlSeconds: schedule.session_ttl_seconds == null ? "" : String(schedule.session_ttl_seconds),
  };
}

export function withoutTaskPayloadFields(payload: Record<string, unknown>): Record<string, unknown> {
  const out = { ...payload };
  delete out.prompt;
  delete out.source_request;
  delete out.prompt_source;
  delete out.script_id;
  delete out.args;
  return out;
}

export function buildSchedulePayload(draft: ScheduleDraft, t: Translate): TriggerSchedule {
  const id = draft.id.trim();
  if (!id) throw new Error("schedule id is required");
  const sessionKind = draft.sessionKind;
  const base: TriggerSchedule = {
    id,
    kind: draft.kind.trim() || `${sessionKind}.task`,
    enabled: draft.enabled,
    target: draft.target.trim() || (sessionKind === "script" ? "" : sessionKind === "agent" ? "agent" : "main"),
    timezone: draft.timezone.trim() || undefined,
    session_kind: sessionKind,
    overlap_policy:draft.overlapPolicy,execution:draft.execution,
    run_at:null,cron:null,every_seconds:null,
    starts_at: draft.startsAt || null, ends_at: draft.endsAt || null,
    anchor_at: draft.anchorAt,
    strategy_id: draft.preserved.strategy_id,
    session_id: null, session_ids: [],
    session_ttl_seconds: null,
  };

  if(draft.cadence==='once'){
    if (!draft.runAt || !draft.runAtInstant) throw new Error(t("errRunAtRequired"));
    // saveDraft resolves the selected time zone through the read-only backend.
    base.run_at = draft.runAtInstant;
  }else if (draft.cadence === "cron") {
    const cron = draft.cron.trim();
    if (!cron) throw new Error("cron is required");
    base.cron = cron;
  } else {
    const seconds = Number(draft.everySeconds);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      throw new Error("interval seconds must be positive");
    }
    base.every_seconds = Math.floor(seconds);
  }

  const delivery = buildDeliveryTargets(draft);
  base.delivery_targets = delivery;
  const ttl = draft.ttlSeconds.trim();
  if (ttl) {
    const parsed = Number(ttl);
    if (!Number.isFinite(parsed) || parsed < 0) {
      throw new Error("ttl seconds must be zero or positive");
    }
    base.session_ttl_seconds = Math.floor(parsed);
  }

  if (sessionKind === "agent") {
    const sourceRequest = draft.sourceRequest.trim();
    const generatedPrompt = draft.generatedPrompt.trim();
    if (draft.usePromptOverride && !draft.overrideAcknowledged) throw new Error("Review the execution override after changing the goal");
    const prompt = draft.usePromptOverride ? generatedPrompt : sourceRequest;
    if (!prompt) throw new Error("agent prompt is required");
    const payload = parseJsonObject(draft.triggerPayloadJson, "payload");
    payload.prompt = prompt;
    if (sourceRequest) payload.source_request = sourceRequest;
    payload.title = draft.title.trim() || sourceRequest.slice(0, 100);
    payload.prompt_source = draft.usePromptOverride ? "operator_override" : "operator";
    base.payload = payload;
    base.target = draft.target.trim() || "agent";
    base.session_mode = draft.sessionMode;
    base.attached_skills = parseCsv(draft.attachedSkills);
    if (draft.sessionMode === "reuse" && draft.sessionId.trim()) {
      base.session_id = draft.sessionId.trim();
    }
    if (draft.sessionMode === "fanout") {
      const ids = parseCsv(draft.sessionIds);
      if (!ids.length) throw new Error("fanout session ids are required");
      base.session_ids = ids;
    }
    return base;
  }

  if (sessionKind === "script") {
    const scriptId = draft.scriptId.trim();
    if (!scriptId) throw new Error("script id is required");
    base.target = draft.target.trim() || `script:${scriptId}`;
    base.payload = {
      ...parseJsonObject(draft.triggerPayloadJson, "payload"),
      title: draft.title.trim() || scriptId,
      script_id: scriptId,
      args: parseJsonObject(draft.scriptArgsJson, "script args"),
    };
    return base;
  }

  base.payload = parseJsonObject(draft.triggerPayloadJson, "payload");
  if (draft.title.trim()) base.payload.title = draft.title.trim();
  base.target = draft.target.trim() || "main";
  return base;
}

export function buildDeliveryTargets(draft: ScheduleDraft): NonNullable<TriggerSchedule["delivery_targets"]> {
  if (!draft.deliveryEdited) return draft.originalDeliveryTargets.map(target => ({ ...target }));
  if (draft.deliveryKind === "none") return [];
  const extras = parseDeliveryExtras(draft.extraDeliveryJson);
  const primary: TriggerDeliveryTarget[] = [];
  if (draft.deliveryKind === "webhook") {
    const url = draft.deliveryUrl.trim();
    if (!url) throw new Error("webhook URL is required");
    primary.push({ ...(draft.originalDeliveryTargets[0] || {}), kind: "webhook", url });
  } else if (draft.deliveryKind === "messages") {
    const channel = draft.deliveryChannel.trim() || "ops";
    primary.push({ ...(draft.originalDeliveryTargets[0] || {}), kind: "messages", channel });
  } else if (draft.deliveryKind === "gateway") {
    const platform = draft.deliveryPlatform.trim() || draft.deliveryChannel.trim() || "telegram";
    primary.push({ ...(draft.originalDeliveryTargets[0] || {}), kind: "gateway", platform, channel: draft.deliveryChannel.trim() || platform });
  }
  // Primary target first, then every pre-existing target carried through
  // untouched — an edit never shrinks the target list behind the user's back.
  return [...primary, ...extras];
}

export function parseDeliveryExtras(text: string): TriggerDeliveryTarget[] {
  const raw = text.trim();
  if (!raw) return [];
  const value = JSON.parse(raw) as unknown;
  if (!Array.isArray(value)) {
    throw new Error("extra delivery targets must be a JSON array");
  }
  return value as TriggerDeliveryTarget[];
}

/** Translated inline error for the extra-delivery-targets JSON field. */
export function deliveryExtrasError(text: string, t: Translate): string | null {
  const raw = text.trim();
  if (!raw) return null;
  try {
    if (!Array.isArray(JSON.parse(raw))) return t("errDeliveryExtraArray");
    return null;
  } catch {
    return t("errJsonInvalid");
  }
}

/** Translated inline error for a JSON textarea; empty input is valid. */
export function jsonFieldError(text: string, t: Translate): string | null {
  const raw = text.trim();
  if (!raw) return null;
  try {
    JSON.parse(raw);
    return null;
  } catch {
    return t("errJsonInvalid");
  }
}
