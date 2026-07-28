"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Advanced, Card, Empty, Pill } from "../Page";
import { Select } from "../Select";
import { clientApi } from "../../lib/clientApi";
import { formatTsShort } from "../../lib/format";
import { confirm as confirmDialog } from "../../lib/dialogs";

const PROMOTION_TARGETS = [
  "static_review",
  "backtested",
  "paper",
  "shadow",
  "canary",
  "live",
] as const;

const EVIDENCE_KINDS = [
  "static_review",
  "backtest",
  "custom_replay",
  "event_replay",
  "backtest_waiver",
  "paper_window",
  "shadow_window",
  "canary_window",
  "protection_check",
  "operator_signoff",
] as const;

type PromotionRecord = Record<string, unknown> & {
  promotion_id?: string;
  strategy_id?: string;
  target?: string;
  state?: string;
  reason?: string;
  ts?: number | string;
  operator?: string;
  notes?: string;
};

function stateTone(state?: string): "ok" | "warn" | "danger" | "neutral" | "brand" {
  switch ((state || "").toLowerCase()) {
    case "approved":
    case "applied":
      return "ok";
    case "needs_evidence":
    case "escalate":
      return "warn";
    case "rejected":
      return "danger";
    case "pending":
      return "brand";
    default:
      return "neutral";
  }
}

function fmtTs(ts: unknown): string {
  if (ts == null) return "–";
  if (typeof ts === "string") return formatTsShort(ts);
  const seconds = Number(ts);
  if (!Number.isFinite(seconds)) return String(ts);
  const ms = seconds > 1e12 ? seconds : seconds * 1000;
  return formatTsShort(new Date(ms).toISOString());
}

interface Props {
  strategyId: string;
  status?: string;
  onError: (msg: string | null) => void;
  onNotice: (msg: string | null) => void;
  onRefresh: () => Promise<void> | void;
}

export function StrategyPromotionCard({
  strategyId,
  status,
  onError,
  onNotice,
  onRefresh,
}: Props) {
  const t = useTranslations("strategyPromotion");
  const [promotions, setPromotions] = useState<PromotionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [target, setTarget] = useState<(typeof PROMOTION_TARGETS)[number]>("paper");
  const [notes, setNotes] = useState("");
  const [evidenceKind, setEvidenceKind] =
    useState<(typeof EVIDENCE_KINDS)[number]>("static_review");
  const [evidencePassed, setEvidencePassed] = useState(true);

  async function loadPromotions() {
    setLoading(true);
    try {
      const res = await clientApi.controlPromotionsList(strategyId, 25);
      setPromotions((res.promotions || []) as PromotionRecord[]);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadPromotions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId]);

  const latest = promotions[0];

  const allowedTargets = useMemo(() => {
    if (!status) return PROMOTION_TARGETS;
    const idx = PROMOTION_TARGETS.indexOf(
      status as (typeof PROMOTION_TARGETS)[number],
    );
    if (idx < 0) return PROMOTION_TARGETS;
    return PROMOTION_TARGETS.slice(idx);
  }, [status]);

  async function requestPromotion() {
    setBusy("request");
    onError(null);
    try {
      const res = await clientApi.controlPromotionRequest({
        strategy_id: strategyId,
        target,
        operator: "dashboard",
        notes: notes || undefined,
      });
      const promotion = res.promotion as PromotionRecord;
      onNotice(
        t("promotionResult", { target: String(promotion.target), state: promotion.state ?? "pending" }),
      );
      await loadPromotions();
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function applyPromotion(record: PromotionRecord) {
    if (!record.promotion_id) return;
    const ok = await confirmDialog({
      message: t("applyConfirm", {
        id: String(record.promotion_id),
        target: String(record.target),
      }),
      tone: "warning",
    });
    if (!ok) return;
    setBusy(record.promotion_id);
    onError(null);
    try {
      await clientApi.controlPromotionApply(String(record.promotion_id));
      onNotice(t("applied", { target: String(record.target) }));
      await loadPromotions();
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function recordEvidence() {
    setBusy("evidence");
    onError(null);
    try {
      await clientApi.controlEvidenceRecord({
        strategy_id: strategyId,
        kind: evidenceKind,
        passed: evidencePassed,
        operator: "dashboard",
        payload: {
          note: notes || undefined,
        },
      });
      onNotice(
        t("evidenceRecorded", { kind: evidenceKind, result: evidencePassed ? t("passed") : t("failed") }),
      );
      await loadPromotions();
      await onRefresh();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    // No card-level Refresh — the page-level refresh already re-pulls the
    // workspace, and both mutations below reload the list themselves.
    <Card title={t("title")} description={t("description")}>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="space-y-3">
          <div className="space-y-1.5 text-xs">
            <div className="text-ink-400">{t("currentState")}</div>
            <div className="flex items-center gap-2">
              <Pill tone="brand">{status || "–"}</Pill>
              {latest ? (
                <span className="text-ink-400">
                  {t("lastDecisionPrefix")}{" "}
                  <Pill tone={stateTone(latest.state)}>{latest.state}</Pill>{" "}
                  {t("for")} <span className="font-mono">{latest.target}</span>
                </span>
              ) : null}
            </div>
          </div>

          {/* Operator forms fold away by default — most visits are
              read-only status checks, not promotion paperwork. */}
          <Advanced title={t("requestPromotion")}>
            <div className="space-y-2">
              <div className="flex gap-2 items-center text-xs">
                <span className="text-ink-400">{t("target")}</span>
                <div className="min-w-[180px]">
                  <Select<(typeof PROMOTION_TARGETS)[number]>
                    value={target}
                    onChange={(value) => setTarget(value)}
                    options={allowedTargets.map((tgt) => ({
                      value: tgt,
                      label: tgt,
                    }))}
                    size="sm"
                    ariaLabel={t("target")}
                  />
                </div>
                <button
                  onClick={requestPromotion}
                  disabled={busy === "request"}
                  className="btn-ghost text-xs"
                >
                  {busy === "request" ? "…" : t("request")}
                </button>
              </div>
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                rows={2}
                placeholder={t("notesPlaceholder")}
                className="w-full bg-ink-900 border border-brand-500/20 rounded-md px-2 py-1 text-xs text-ink-200"
              />
            </div>
          </Advanced>

          <Advanced title={t("recordEvidence")}>
            <div className="flex gap-2 items-center text-xs flex-wrap">
              <span className="text-ink-400">{t("kind")}</span>
              <div className="min-w-[180px]">
                <Select<(typeof EVIDENCE_KINDS)[number]>
                  value={evidenceKind}
                  onChange={(value) => setEvidenceKind(value)}
                  options={EVIDENCE_KINDS.map((kind) => ({
                    value: kind,
                    label: kind,
                  }))}
                  size="sm"
                  ariaLabel={t("kind")}
                />
              </div>
              <label className="flex items-center gap-1.5 text-ink-300">
                <input
                  type="checkbox"
                  checked={evidencePassed}
                  onChange={(e) => setEvidencePassed(e.target.checked)}
                />
                {t("passed")}
              </label>
              <button
                onClick={recordEvidence}
                disabled={busy === "evidence"}
                className="btn-ghost text-xs"
              >
                {busy === "evidence" ? "…" : t("record")}
              </button>
            </div>
          </Advanced>
        </div>

        <div>
          <div className="text-[12px] text-ink-400 font-medium mb-2">
            {t("recentPromotions")}
          </div>
          {promotions.length === 0 ? (
            <Empty
              label={loading ? t("loading") : t("noRecords")}
            />
          ) : (
            <div className="embedded-list-scroll space-y-1.5">
              {promotions.map((rec) => (
                <div
                  key={String(rec.promotion_id ?? Math.random())}
                  className="flex items-center gap-2 text-xs border border-brand-500/10 rounded px-2 py-1.5 bg-ink-900/40"
                >
                  <Pill tone={stateTone(rec.state)}>{rec.state || "?"}</Pill>
                  <span className="font-mono">{rec.target}</span>
                  <span className="text-ink-500 truncate">{rec.reason || ""}</span>
                  <span className="ml-auto text-ink-500 font-mono shrink-0">
                    {fmtTs(rec.ts)}
                  </span>
                  {rec.state === "approved" ? (
                    <button
                      onClick={() => applyPromotion(rec)}
                      disabled={busy === rec.promotion_id}
                      className="btn-ghost text-[11px] py-0.5 text-accent-300"
                    >
                      {busy === rec.promotion_id ? "…" : t("apply")}
                    </button>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
