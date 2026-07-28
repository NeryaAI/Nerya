"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Json,
  Kpi,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import {
  PauseIcon,
  PlusIcon,
  RefreshIcon,
  SendIcon,
  TrashIcon,
  WrenchIcon,
} from "../../components/icons";
import { confirm as confirmDialog, toast } from "../../lib/dialogs";
import {
  describeCron,
  describeIntervalSeconds,
  formatTsShort,
} from "../../lib/format";
import { clientApi } from "../../lib/clientApi";
import type { TriggerRoute, TriggerSchedule } from "../../lib/clientApi";

type ScheduleKind = "agent" | "script" | "trigger";
type CadenceKind = "cron" | "interval";
type DeliveryKind = "none" | "gateway" | "messages" | "webhook";
type ScheduleFilter = "all" | "agent" | "script" | "trigger" | "active" | "paused";
type Translate = (key: string, values?: Record<string, string | number>) => string;

type ScheduleDraft = {
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
  ttlSeconds: string;
};

const EMPTY_OBJECT = "{}";

function blankDraft(): ScheduleDraft {
  return {
    id: "",
    kind: "agent.task",
    enabled: true,
    cadence: "cron",
    cron: "0 11 * * *",
    everySeconds: "3600",
    timezone: "Asia/Shanghai",
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
    ttlSeconds: "",
  };
}

function scheduleKind(schedule: TriggerSchedule): ScheduleKind {
  const raw = String(schedule.session_kind || "").toLowerCase();
  if (raw === "agent" || raw === "script") return raw;
  return "trigger";
}

function isPaused(schedule: TriggerSchedule): boolean {
  return Boolean(schedule.paused ?? schedule.enabled === false);
}

function parseJsonObject(text: string, label: string): Record<string, unknown> {
  const raw = text.trim();
  if (!raw) return {};
  const value = JSON.parse(raw) as unknown;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value as Record<string, unknown>;
}

function parseCsv(text: string): string[] {
  return text
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function titleFor(schedule: TriggerSchedule): string {
  const payload = schedule.payload || {};
  const title = payload.title || payload.source_request || schedule.description || schedule.title;
  return String(title || schedule.id);
}

function cadenceFor(schedule: TriggerSchedule): string {
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
function useCadenceLabel(): (schedule: TriggerSchedule) => string {
  const t = useTranslations("cadence");
  return (schedule) => {
    const desc = schedule.cron
      ? describeCron(String(schedule.cron))
      : schedule.every_seconds != null
        ? describeIntervalSeconds(Number(schedule.every_seconds))
        : null;
    if (!desc) return cadenceFor(schedule);
    return t(desc.key, (desc.params ?? {}) as Record<string, string | number>);
  };
}

function deliverySummary(schedule: TriggerSchedule): string {
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

function statusTone(schedule: TriggerSchedule): "ok" | "brand" | "warn" {
  if (isPaused(schedule)) return "brand";
  return schedule.last_fired_ts ? "ok" : "warn";
}

function buildDraftFromSchedule(schedule: TriggerSchedule): ScheduleDraft {
  const kind = scheduleKind(schedule);
  const payload = schedule.payload || {};
  const firstDelivery = (schedule.delivery_targets || [])[0] || null;
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
    id: schedule.id,
    kind: schedule.kind || `${kind}.task`,
    enabled: !isPaused(schedule),
    cadence: schedule.cron ? "cron" : "interval",
    cron: schedule.cron || "0 11 * * *",
    everySeconds: String(schedule.every_seconds ?? 3600),
    timezone: schedule.timezone || "Asia/Shanghai",
    sessionKind: kind,
    sessionMode: schedule.session_mode || "ephemeral",
    sessionId: schedule.session_id || "",
    sessionIds: (schedule.session_ids || []).join(", "),
    attachedSkills: (schedule.attached_skills || []).join(", "),
    sourceRequest: String(payload.source_request || ""),
    generatedPrompt: String(payload.prompt || ""),
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
    ttlSeconds: schedule.session_ttl_seconds == null ? "" : String(schedule.session_ttl_seconds),
  };
}

function withoutTaskPayloadFields(payload: Record<string, unknown>): Record<string, unknown> {
  const out = { ...payload };
  delete out.prompt;
  delete out.source_request;
  delete out.prompt_source;
  delete out.script_id;
  delete out.args;
  return out;
}

function buildSchedulePayload(draft: ScheduleDraft): TriggerSchedule {
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
  };

  if (draft.cadence === "cron") {
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
  if (delivery.length) base.delivery_targets = delivery;
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
    const prompt = generatedPrompt || sourceRequest;
    if (!prompt) throw new Error("agent prompt is required");
    const payload = parseJsonObject(draft.triggerPayloadJson, "payload");
    payload.prompt = prompt;
    if (sourceRequest) payload.source_request = sourceRequest;
    payload.prompt_source = generatedPrompt ? "agent_generated" : "prompt_fallback";
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
      script_id: scriptId,
      args: parseJsonObject(draft.scriptArgsJson, "script args"),
    };
    return base;
  }

  base.payload = parseJsonObject(draft.triggerPayloadJson, "payload");
  base.target = draft.target.trim() || "main";
  return base;
}

function buildDeliveryTargets(draft: ScheduleDraft): NonNullable<TriggerSchedule["delivery_targets"]> {
  if (draft.deliveryKind === "none") return [];
  if (draft.deliveryKind === "webhook") {
    const url = draft.deliveryUrl.trim();
    if (!url) throw new Error("webhook URL is required");
    return [{ kind: "webhook", url }];
  }
  if (draft.deliveryKind === "messages") {
    const channel = draft.deliveryChannel.trim() || "ops";
    return [{ kind: "messages", channel }];
  }
  const platform = draft.deliveryPlatform.trim() || draft.deliveryChannel.trim() || "telegram";
  return [{ kind: "gateway", platform, channel: draft.deliveryChannel.trim() || platform }];
}

export default function WorkflowsPage() {
  const t = useTranslations("workflows") as Translate;
  const tCommon = useTranslations("common") as Translate;
  const [routes, setRoutes] = useState<TriggerRoute[]>([]);
  const [schedules, setSchedules] = useState<TriggerSchedule[]>([]);
  const [statusRows, setStatusRows] = useState<TriggerSchedule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<ScheduleFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftOpen, setDraftOpen] = useState(false);
  const [draftMode, setDraftMode] = useState<"create" | "edit">("create");
  const [draft, setDraft] = useState<ScheduleDraft>(() => blankDraft());

  const load = useCallback(async () => {
    try {
      const [r, s, st] = await Promise.all([
        clientApi.triggerRoutes().catch(() => ({ routes: [] })),
        clientApi.triggerSchedules().catch(() => ({ schedules: [] })),
        clientApi.scheduleStatuses().catch(() => ({ schedules: [] })),
      ]);
      const routes_ = Array.isArray(r) ? r : (r as { routes?: TriggerRoute[] }).routes ?? [];
      const schedules_ = s.schedules ?? [];
      const status_ = st.schedules ?? [];
      setRoutes(routes_);
      setSchedules(schedules_);
      setStatusRows(status_);
      setSelectedId((prev) => {
        if (prev && schedules_.some((row) => row.id === prev)) return prev;
        return schedules_[0]?.id ?? null;
      });
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);

  const rows = useMemo(() => {
    const statusById = new Map(statusRows.map((row) => [row.id, row]));
    return schedules.map((row) => ({ ...row, ...(statusById.get(row.id) || {}) }));
  }, [schedules, statusRows]);

  const filteredRows = useMemo(() => {
    return rows.filter((row) => {
      if (filter === "all") return true;
      if (filter === "active") return !isPaused(row);
      if (filter === "paused") return isPaused(row);
      return scheduleKind(row) === filter;
    });
  }, [rows, filter]);

  const selected = useMemo(
    () => rows.find((row) => row.id === selectedId) ?? filteredRows[0] ?? rows[0] ?? null,
    [filteredRows, rows, selectedId],
  );

  const counts = useMemo(() => {
    return rows.reduce(
      (acc, row) => {
        acc.total += 1;
        if (isPaused(row)) acc.paused += 1;
        else acc.active += 1;
        acc[scheduleKind(row)] += 1;
        return acc;
      },
      { total: 0, active: 0, paused: 0, agent: 0, script: 0, trigger: 0 },
    );
  }, [rows]);

  function openCreate(kind: ScheduleKind = "agent") {
    const next = blankDraft();
    next.id = `task_${Date.now().toString(36)}`;
    next.sessionKind = kind;
    next.kind = `${kind}.task`;
    if (kind === "script") {
      next.target = "script:";
      next.cadence = "cron";
    } else if (kind === "trigger") {
      next.target = "main";
      next.sessionMode = "ephemeral";
    } else {
      next.target = "agent";
    }
    setDraft(next);
    setDraftMode("create");
    setDraftOpen(true);
  }

  function openEdit(schedule: TriggerSchedule) {
    setDraft(buildDraftFromSchedule(schedule));
    setDraftMode("edit");
    setDraftOpen(true);
  }

  async function saveDraft() {
    setBusy("save");
    try {
      const payload = buildSchedulePayload(draft);
      if (draftMode === "edit") {
        await clientApi.scheduleUpdate(payload.id, payload);
        toast({ message: t("savedSchedule", { id: payload.id }), tone: "ok" });
      } else {
        await clientApi.scheduleAdd(payload);
        toast({ message: t("createdSchedule", { id: payload.id }), tone: "ok" });
      }
      setDraftOpen(false);
      setSelectedId(payload.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function toggleSchedule(schedule: TriggerSchedule) {
    setBusy(`toggle:${schedule.id}`);
    try {
      if (isPaused(schedule)) {
        await clientApi.scheduleResume(schedule.id);
      } else {
        await clientApi.schedulePause(schedule.id);
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function runSchedule(schedule: TriggerSchedule) {
    if (isStrategySchedule(schedule)) {
      const ok = await confirmDialog({
        title: t("runStrategyTitle"),
        message: t("runStrategyConfirm", { id: schedule.id }),
        okLabel: t("runNow"),
        cancelLabel: tCommon("cancel"),
        tone: "warning",
      });
      if (!ok) return;
    }
    setBusy(`run:${schedule.id}`);
    try {
      const result = await clientApi.scheduleRunNow(schedule.id);
      toast({
        message: result.ok
          ? t("runQueued", { id: schedule.id })
          : t("runFailed", { id: schedule.id }),
        tone: result.ok ? "ok" : "error",
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function removeSchedule(schedule: TriggerSchedule) {
    const ok = await confirmDialog({
      title: t("deleteTitle"),
      message: t("deleteConfirm", { id: schedule.id }),
      okLabel: tCommon("delete"),
      cancelLabel: tCommon("cancel"),
      tone: "danger",
    });
    if (!ok) return;
    setBusy(`delete:${schedule.id}`);
    try {
      await clientApi.scheduleRemove(schedule.id);
      if (selectedId === schedule.id) setSelectedId(null);
      await load();
      toast({ message: t("deletedSchedule", { id: schedule.id }), tone: "ok" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function tickSchedules() {
    setBusy("tick");
    try {
      const result = await clientApi.scheduleTick();
      toast({
        message: t("tickResult", { count: Array.isArray(result.fired) ? result.fired.length : 0 }),
        tone: "brand",
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const filters: { id: ScheduleFilter; label: string }[] = [
    { id: "all", label: t("filterAll") },
    { id: "agent", label: t("kindAgent") },
    { id: "script", label: t("kindScript") },
    { id: "trigger", label: t("kindTrigger") },
    { id: "active", label: t("active") },
    { id: "paused", label: t("paused") },
  ];

  return (
    <div>
      {error ? <ErrorBanner error={error} /> : null}
      <PageBody>
        <PageHeader
          eyebrow={t("eyebrow")}
          title={t("title")}
          description={t("description")}
          actions={
            <div className="flex items-center gap-2">
              {/* "+ New task" was removed here — the Scheduled-tasks card
                  already offers the typed entry points (+ Agent / + Script /
                  + Trigger), and two create paths for one action doubled the
                  learning cost. "Run scheduler tick" is a debug affordance,
                  so it renders icon-only and quiet. */}
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => void load()}
              >
                <RefreshIcon size={14} />
                {tCommon("refresh")}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy === "tick"}
                onClick={() => void tickSchedules()}
                title={t("tickNow")}
                aria-label={t("tickNow")}
              >
                <WrenchIcon size={14} />
                {busy === "tick" ? tCommon("working") : null}
              </button>
            </div>
          }
        />
        <SectionTabs section="strategy" />

        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <Kpi inline label={t("kpiTotal")} value={counts.total} />
          <Kpi inline label={t("kpiActive")} value={counts.active} tone="ok" />
          <Kpi inline label={t("kpiAgent")} value={counts.agent} tone="brand" />
          <Kpi inline label={t("kpiScript")} value={counts.script} tone="warn" />
          <Kpi inline label={t("kpiRoutes")} value={routes.length} />
        </div>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(360px,0.95fr)_minmax(0,1.35fr)]">
          <Card
            title={t("schedulesTitle")}
            description={t("schedulesDesc")}
            actions={
              <div className="flex flex-wrap items-center gap-1">
                {(["agent", "script", "trigger"] as ScheduleKind[]).map((kind) => (
                  <button
                    key={kind}
                    type="button"
                    className="btn-ghost px-2 py-1 text-[11px]"
                    onClick={() => openCreate(kind)}
                  >
                    <PlusIcon size={12} />
                    {kindLabel(kind, t)}
                  </button>
                ))}
              </div>
            }
            padded={false}
          >
            <div className="border-b border-brand-500/10 px-3 py-2">
              <div className="flex flex-wrap items-center gap-1">
                {filters.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setFilter(item.id)}
                    className={[
                      "rounded-md border px-2.5 py-1 text-[12px] transition",
                      filter === item.id
                        ? "border-brand-500/40 bg-brand-500/15 text-brand-100"
                        : "border-transparent text-ink-400 hover:border-brand-500/20 hover:text-ink-200",
                    ].join(" ")}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>

            {loading && !rows.length ? (
              <div className="p-4 text-[12px] text-ink-500">{t("loadingEllipsis")}</div>
            ) : filteredRows.length === 0 ? (
              <Empty label={t("noSchedules")} />
            ) : (
              <ul className="embedded-list-scroll-lg">
                {filteredRows.map((schedule) => (
                  <ScheduleListItem
                    key={schedule.id}
                    schedule={schedule}
                    active={selected?.id === schedule.id}
                    busy={busy}
                    onSelect={() => setSelectedId(schedule.id)}
                    onRun={() => void runSchedule(schedule)}
                    onToggle={() => void toggleSchedule(schedule)}
                  />
                ))}
              </ul>
            )}
          </Card>

          <div className="space-y-4">
            {draftOpen ? (
              <ScheduleEditor
                mode={draftMode}
                draft={draft}
                busy={busy}
                t={t}
                tCommon={tCommon}
                onChange={setDraft}
                onCancel={() => setDraftOpen(false)}
                onSave={() => void saveDraft()}
              />
            ) : selected ? (
              <ScheduleDetail
                schedule={selected}
                busy={busy}
                routes={routes}
                t={t}
                tCommon={tCommon}
                onEdit={() => openEdit(selected)}
                onRun={() => void runSchedule(selected)}
                onToggle={() => void toggleSchedule(selected)}
                onDelete={() => void removeSchedule(selected)}
              />
            ) : (
              <Card title={t("selectSchedule")}>
                <Empty label={t("selectScheduleHint")} />
              </Card>
            )}

            <Card
              title={t("routesTitle")}
              description={t("routesDesc")}
              padded={false}
            >
              {loading && routes.length === 0 ? (
                <div className="p-4 text-[12px] text-ink-500">{t("loadingEllipsis")}</div>
              ) : routes.length === 0 ? (
                <Empty label={t("noRoutes")} />
              ) : (
                <ul className="embedded-list-scroll">
                  {routes.map((route) => (
                    <RouteListItem key={route.id} route={route} t={t} />
                  ))}
                </ul>
              )}
            </Card>
          </div>
        </div>
      </PageBody>
    </div>
  );
}

function isStrategySchedule(schedule: TriggerSchedule): boolean {
  const kind = String(schedule.kind || "");
  const target = String(schedule.target || "");
  return Boolean(
    schedule.strategy_id ||
      kind.startsWith("strategy.") ||
      target.includes("strategy"),
  );
}

function ScheduleListItem({
  schedule,
  active,
  busy,
  onSelect,
  onRun,
  onToggle,
}: {
  schedule: TriggerSchedule;
  active: boolean;
  busy: string | null;
  onSelect: () => void;
  onRun: () => void;
  onToggle: () => void;
}) {
  const paused = isPaused(schedule);
  const kind = scheduleKind(schedule);
  const cadenceLabel = useCadenceLabel();
  const t = useTranslations("workflows");
  return (
    <li
      className={[
        "border-b border-brand-500/5 px-3 py-2.5 last:border-b-0",
        active ? "bg-brand-500/10" : "hover:bg-brand-500/5",
      ].join(" ")}
    >
      <button type="button" onClick={onSelect} className="block w-full min-w-0 text-left">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${
              paused ? "bg-ink-500" : kind === "script" ? "bg-amber-400" : kind === "agent" ? "bg-brand-300" : "bg-emerald-400"
            }`}
          />
          <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-100">
            {titleFor(schedule)}
          </span>
          <Pill tone={paused ? "brand" : "ok"}>{paused ? t("paused") : t("active")}</Pill>
        </div>
        <div className="mt-1 flex min-w-0 items-center gap-2 text-[10.5px] text-ink-500">
          <span className="font-mono truncate">{schedule.id}</span>
          <span>·</span>
          <span className="shrink-0">{cadenceLabel(schedule)}</span>
          <span>·</span>
          <span className="shrink-0">{kind}</span>
        </div>
      </button>
      <div className="mt-2 flex items-center gap-1">
        <button
          type="button"
          className="btn-ghost px-2 py-0.5 text-[11px]"
          disabled={busy === `run:${schedule.id}`}
          onClick={onRun}
          title={t("runNow")}
        >
          <SendIcon size={12} />
        </button>
        <button
          type="button"
          className="btn-ghost px-2 py-0.5 text-[11px]"
          disabled={busy === `toggle:${schedule.id}`}
          onClick={onToggle}
          title={paused ? t("resume") : t("pause")}
        >
          <PauseIcon size={12} />
        </button>
      </div>
    </li>
  );
}

function ScheduleDetail({
  schedule,
  busy,
  routes,
  t,
  tCommon,
  onEdit,
  onRun,
  onToggle,
  onDelete,
}: {
  schedule: TriggerSchedule;
  busy: string | null;
  routes: TriggerRoute[];
  t: Translate;
  tCommon: Translate;
  onEdit: () => void;
  onRun: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const kind = scheduleKind(schedule);
  const paused = isPaused(schedule);
  const cadenceLabel = useCadenceLabel();
  const matchingRoutes = routes.filter((route) => {
    const matchKind = typeof route.match?.kind === "string" ? route.match.kind : "";
    return matchKind === schedule.kind || matchKind === "*";
  });
  return (
    <Card
      title={titleFor(schedule)}
      description={schedule.id}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={statusTone(schedule)}>{paused ? t("paused") : t("active")}</Pill>
          <button type="button" className="btn-ghost text-xs" onClick={onEdit}>
            <WrenchIcon size={13} />
            {tCommon("edit")}
          </button>
          <button
            type="button"
            className="btn-ghost text-xs"
            disabled={busy === `run:${schedule.id}`}
            onClick={onRun}
          >
            <SendIcon size={13} />
            {t("runNow")}
          </button>
          <button
            type="button"
            className="btn-ghost text-xs"
            disabled={busy === `toggle:${schedule.id}`}
            onClick={onToggle}
          >
            <PauseIcon size={13} />
            {paused ? t("resume") : t("pause")}
          </button>
          <button
            type="button"
            className="btn-ghost text-xs text-rose-300"
            disabled={busy === `delete:${schedule.id}`}
            onClick={onDelete}
          >
            <TrashIcon size={13} />
            {tCommon("delete")}
          </button>
        </div>
      }
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label={t("metricKind")} value={kindLabel(kind, t)} />
        <Metric label={t("metricCadence")} value={cadenceLabel(schedule)} />
        <Metric label={t("metricTimezone")} value={schedule.timezone || "-"} mono />
        <Metric label={t("metricLastRun")} value={formatTsShort(schedule.last_fired_ts ?? undefined)} />
        <Metric label={t("metricTarget")} value={schedule.target || "-"} mono />
        <Metric label={t("metricDelivery")} value={deliverySummary(schedule)} mono />
        <Metric label={t("metricSession")} value={schedule.session_mode || "-"} mono />
        <Metric
          label={t("metricRoutes")}
          value={matchingRoutes.length ? String(matchingRoutes.length) : "-"}
        />
      </div>

      {kind === "agent" ? (
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <TextBlock label={t("sourceRequest")} value={String(schedule.payload?.source_request || "-")} />
          <TextBlock label={t("agentPrompt")} value={String(schedule.payload?.prompt || "-")} />
        </div>
      ) : null}

      {kind === "script" ? (
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <Metric label={t("scriptId")} value={String(schedule.payload?.script_id || "-")} mono />
          <PayloadTable value={(schedule.payload?.args || {}) as Record<string, unknown>} />
        </div>
      ) : null}

      <Advanced title={t("rawSchedule")} defaultOpen={false}>
        <Json value={schedule} />
      </Advanced>
    </Card>
  );
}

function ScheduleEditor({
  mode,
  draft,
  busy,
  t,
  tCommon,
  onChange,
  onCancel,
  onSave,
}: {
  mode: "create" | "edit";
  draft: ScheduleDraft;
  busy: string | null;
  t: Translate;
  tCommon: Translate;
  onChange: (draft: ScheduleDraft) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  const patch = (next: Partial<ScheduleDraft>) => onChange({ ...draft, ...next });
  return (
    <Card
      title={mode === "create" ? t("createTitle") : t("editTitle")}
      description={mode === "edit" ? draft.id : t("createDesc")}
      actions={
        <div className="flex items-center gap-2">
          <button type="button" className="btn-ghost text-xs" onClick={onCancel}>
            {tCommon("cancel")}
          </button>
          <button
            type="button"
            className="btn-primary text-xs"
            disabled={busy === "save"}
            onClick={onSave}
          >
            {busy === "save" ? tCommon("saving") : tCommon("save")}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-1 rounded-lg border border-brand-500/10 bg-ink-950/25 p-1">
          {(["agent", "script", "trigger"] as ScheduleKind[]).map((kind) => (
            <button
              key={kind}
              type="button"
              disabled={mode === "edit"}
              onClick={() =>
                patch({
                  sessionKind: kind,
                  kind: `${kind}.task`,
                  target: kind === "script" ? "script:" : kind === "agent" ? "agent" : "main",
                })
              }
              className={[
                "rounded-md px-3 py-1.5 text-[12px] transition disabled:cursor-not-allowed disabled:opacity-60",
                draft.sessionKind === kind
                  ? "bg-brand-500/20 text-brand-100"
                  : "text-ink-400 hover:bg-brand-500/10 hover:text-ink-100",
              ].join(" ")}
            >
              {kindLabel(kind, t)}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <Field label={t("fieldId")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.id}
              disabled={mode === "edit"}
              onChange={(e) => patch({ id: e.target.value })}
            />
          </Field>
          <Field label={t("fieldKind")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.kind}
              onChange={(e) => patch({ kind: e.target.value })}
            />
          </Field>
          <Field label={t("fieldCadence")}>
            <select
              className="input-dark text-xs"
              value={draft.cadence}
              onChange={(e) => patch({ cadence: e.target.value as CadenceKind })}
            >
              <option value="cron">{t("cadenceCron")}</option>
              <option value="interval">{t("cadenceInterval")}</option>
            </select>
          </Field>
          <Field label={draft.cadence === "cron" ? t("fieldCron") : t("fieldEverySeconds")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.cadence === "cron" ? draft.cron : draft.everySeconds}
              onChange={(e) =>
                draft.cadence === "cron"
                  ? patch({ cron: e.target.value })
                  : patch({ everySeconds: e.target.value })
              }
            />
          </Field>
          <Field label={t("fieldTimezone")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.timezone}
              onChange={(e) => patch({ timezone: e.target.value })}
            />
          </Field>
          <Field label={t("fieldTarget")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.target}
              onChange={(e) => patch({ target: e.target.value })}
            />
          </Field>
        </div>

        {draft.sessionKind === "agent" ? (
          <AgentFields draft={draft} patch={patch} t={t} />
        ) : null}
        {draft.sessionKind === "script" ? (
          <ScriptFields draft={draft} patch={patch} t={t} />
        ) : null}
        {draft.sessionKind === "trigger" ? (
          <Field label={t("fieldPayload")}>
            <textarea
              className="input-dark min-h-[160px] w-full font-mono text-xs"
              value={draft.triggerPayloadJson}
              onChange={(e) => patch({ triggerPayloadJson: e.target.value })}
            />
          </Field>
        ) : null}

        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <Field label={t("fieldDeliveryKind")}>
            <select
              className="input-dark text-xs"
              value={draft.deliveryKind}
              onChange={(e) => patch({ deliveryKind: e.target.value as DeliveryKind })}
            >
              <option value="none">{t("deliveryNone")}</option>
              <option value="gateway">{t("deliveryGateway")}</option>
              <option value="messages">{t("deliveryMessages")}</option>
              <option value="webhook">{t("deliveryWebhook")}</option>
            </select>
          </Field>
          {draft.deliveryKind === "gateway" ? (
            <Field label={t("fieldDeliveryPlatform")}>
              <input
                className="input-dark font-mono text-xs"
                value={draft.deliveryPlatform}
                onChange={(e) => patch({ deliveryPlatform: e.target.value })}
              />
            </Field>
          ) : null}
          {draft.deliveryKind === "messages" || draft.deliveryKind === "gateway" ? (
            <Field label={t("fieldDeliveryChannel")}>
              <input
                className="input-dark font-mono text-xs"
                value={draft.deliveryChannel}
                onChange={(e) => patch({ deliveryChannel: e.target.value })}
              />
            </Field>
          ) : null}
          {draft.deliveryKind === "webhook" ? (
            <Field label={t("fieldDeliveryUrl")}>
              <input
                className="input-dark font-mono text-xs"
                value={draft.deliveryUrl}
                onChange={(e) => patch({ deliveryUrl: e.target.value })}
              />
            </Field>
          ) : null}
        </div>

        <label className="flex items-center gap-2 text-[12px] text-ink-300">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => patch({ enabled: e.target.checked })}
          />
          {t("fieldEnabled")}
        </label>
      </div>
    </Card>
  );
}

function AgentFields({
  draft,
  patch,
  t,
}: {
  draft: ScheduleDraft;
  patch: (next: Partial<ScheduleDraft>) => void;
  t: Translate;
}) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <Field label={t("fieldSessionMode")}>
          <select
            className="input-dark text-xs"
            value={draft.sessionMode}
            onChange={(e) => patch({ sessionMode: e.target.value as ScheduleDraft["sessionMode"] })}
          >
            <option value="ephemeral">{t("sessionEphemeral")}</option>
            <option value="reuse">{t("sessionReuse")}</option>
            <option value="fanout">{t("sessionFanout")}</option>
          </select>
        </Field>
        {draft.sessionMode === "reuse" ? (
          <Field label={t("fieldSessionId")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.sessionId}
              onChange={(e) => patch({ sessionId: e.target.value })}
            />
          </Field>
        ) : null}
        {draft.sessionMode === "fanout" ? (
          <Field label={t("fieldSessionIds")}>
            <input
              className="input-dark font-mono text-xs"
              value={draft.sessionIds}
              onChange={(e) => patch({ sessionIds: e.target.value })}
            />
          </Field>
        ) : null}
        <Field label={t("fieldAttachedSkills")}>
          <input
            className="input-dark font-mono text-xs"
            value={draft.attachedSkills}
            onChange={(e) => patch({ attachedSkills: e.target.value })}
          />
        </Field>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <Field label={t("sourceRequest")}>
          <textarea
            className="input-dark min-h-[140px] w-full text-xs"
            value={draft.sourceRequest}
            onChange={(e) => patch({ sourceRequest: e.target.value })}
          />
        </Field>
        <Field label={t("agentPrompt")}>
          <textarea
            className="input-dark min-h-[140px] w-full font-mono text-xs"
            value={draft.generatedPrompt}
            onChange={(e) => patch({ generatedPrompt: e.target.value })}
          />
        </Field>
      </div>
      <Field label={t("fieldPayloadExtra")}>
        <textarea
          className="input-dark min-h-[120px] w-full font-mono text-xs"
          value={draft.triggerPayloadJson}
          onChange={(e) => patch({ triggerPayloadJson: e.target.value })}
        />
      </Field>
    </div>
  );
}

function ScriptFields({
  draft,
  patch,
  t,
}: {
  draft: ScheduleDraft;
  patch: (next: Partial<ScheduleDraft>) => void;
  t: Translate;
}) {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      <Field label={t("scriptId")}>
        <input
          className="input-dark font-mono text-xs"
          value={draft.scriptId}
          onChange={(e) => patch({ scriptId: e.target.value })}
        />
      </Field>
      <Field label={t("scriptArgs")}>
        <textarea
          className="input-dark min-h-[120px] w-full font-mono text-xs"
          value={draft.scriptArgsJson}
          onChange={(e) => patch({ scriptArgsJson: e.target.value })}
        />
      </Field>
    </div>
  );
}

function RouteListItem({ route, t }: { route: TriggerRoute; t: Translate }) {
  const matchKind = typeof route.match?.kind === "string" ? route.match.kind : "any";
  const skill = typeof route.action?.skill_id === "string" ? route.action.skill_id : "?";
  return (
    <li className="border-b border-brand-500/5 px-3 py-2.5 last:border-b-0">
      <div className="flex min-w-0 items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${route.paused ? "bg-ink-500" : "bg-emerald-500"}`} />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-100">
          {route.title || route.id}
        </span>
        <Pill tone={route.paused ? "brand" : "ok"}>
          {route.paused ? t("paused") : t("active")}
        </Pill>
      </div>
      <div className="mt-1 text-[10.5px] font-mono text-ink-500">
        {matchKind} -&gt; {skill}
      </div>
    </li>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block min-w-0 text-[12px] text-ink-400">
      <span className="mb-1 block text-ink-300">{label}</span>
      {children}
    </label>
  );
}

function Metric({
  label,
  value,
  mono,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-brand-500/10 bg-ink-950/20 px-3 py-2">
      <div className="text-[11px] text-ink-500">{label}</div>
      <div className={`mt-1 truncate text-[12px] text-ink-100 ${mono ? "font-mono" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function TextBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-brand-500/10 bg-ink-950/20 px-3 py-2">
      <div className="text-[11px] text-ink-500">{label}</div>
      <div className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap text-[12px] leading-relaxed text-ink-200">
        {value}
      </div>
    </div>
  );
}

function PayloadTable({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  if (!entries.length) return <Metric label="args" value="-" mono />;
  return (
    <div className="overflow-hidden rounded-lg border border-brand-500/10">
      <table className="w-full text-left text-[12px]">
        <tbody>
          {entries.map(([key, raw]) => (
            <tr key={key} className="border-b border-brand-500/5 last:border-b-0">
              <th className="w-36 bg-ink-950/30 px-3 py-2 font-mono font-normal text-ink-500">
                {key}
              </th>
              <td className="px-3 py-2 font-mono text-ink-200">
                {typeof raw === "string" || typeof raw === "number" || typeof raw === "boolean"
                  ? String(raw)
                  : JSON.stringify(raw)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function kindLabel(kind: ScheduleKind, t: Translate): string {
  if (kind === "agent") return t("kindAgent");
  if (kind === "script") return t("kindScript");
  return t("kindTrigger");
}
