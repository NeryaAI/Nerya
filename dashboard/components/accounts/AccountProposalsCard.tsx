"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Empty, Pill } from "../Page";
import { JsonView } from "../JsonView";
import {
  clientApi,
  type AccountProposalView,
} from "../../lib/clientApi";
import { confirm as confirmDialog, prompt as promptDialog } from "../../lib/dialogs";

interface Props {
  onApplied?: () => void;
}

function formatTs(ts: string): string {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    if (!Number.isFinite(d.getTime())) return ts;
    return d.toLocaleString();
  } catch {
    return ts;
  }
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "—";
  return String(value);
}

function DiffValue({ value, tone }: { value: unknown; tone: "before" | "after" }) {
  if (value && typeof value === "object") {
    return (
      <JsonView
        value={value}
        showRawToggle={false}
        initialCollapsed
        maxDepth={2}
        className="!border-0 !bg-transparent !px-0 !py-0"
      />
    );
  }
  const toneClass = tone === "after" ? "text-amber-200" : "text-ink-400";
  return (
    <span className={`break-all font-mono text-[11px] ${toneClass}`}>
      {formatValue(value)}
    </span>
  );
}

/**
 * Pending account proposal review card.
 *
 * Shows account_roster_patch proposals staged via /accounts/upsert
 * with ``apply: false``. The operator can approve (writes the YAML
 * via the same vault-only path the direct upsert uses) or reject
 * the proposal without touching accounts.yml at all.
 */
export function AccountProposalsCard({ onApplied }: Props) {
  const t = useTranslations("accountProposals");
  const [proposals, setProposals] = useState<AccountProposalView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await clientApi.accountsProposalsList("pending_review");
      setProposals(res.proposals ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, []);

  async function approve(p: AccountProposalView) {
    const confirmed = await confirmDialog({
      message: t("approveConfirm", {
        id: p.id,
        operation: p.operation,
        target: p.target_id,
        count: Object.keys(p.diff || {}).length,
      }),
      tone: "brand",
    });
    if (!confirmed) return;
    setBusy(`${p.id}:apply`);
    setError(null);
    try {
      const res = await clientApi.accountsProposalApply({
        proposal_id: p.id,
        operator: "dashboard",
      });
      if (!res.ok) throw new Error(res.detail || res.error || "apply_failed");
      await load();
      onApplied?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function reject(p: AccountProposalView) {
    const note = await promptDialog({
      message: t("rejectPrompt", { id: p.id }),
      defaultValue: "",
    });
    if (note == null) return;
    setBusy(`${p.id}:reject`);
    setError(null);
    try {
      const res = await clientApi.accountsProposalReject({
        proposal_id: p.id,
        operator: "dashboard",
        note,
      });
      if (!res.ok) throw new Error(res.detail || res.error || "reject_failed");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (proposals.length === 0 && !loading && !error) {
    return null;
  }

  return (
    <Card
      title={t("title", { count: proposals.length })}
      description={t("description")}
      actions={
        <button
          onClick={() => void load()}
          disabled={loading}
          className="btn-ghost text-xs"
        >
          {loading ? "…" : t("refresh")}
        </button>
      }
    >
      {error && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      )}
      {proposals.length === 0 ? (
        <Empty label={t("noPending")} />
      ) : (
        <div className="embedded-list-scroll-lg grid gap-2">
          {proposals.map((p) => {
            const isOpen = expanded === p.id;
            const fieldCount = Object.keys(p.diff || {}).length;
            return (
              <div
                key={p.id}
                className="rounded-xl border border-amber-400/20 bg-amber-400/5 p-3 text-xs"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone="warn">{p.state}</Pill>
                  <Pill tone="brand">{p.operation}</Pill>
                  <span className="font-mono text-ink-200">
                    {p.target_id}
                  </span>
                  <span className="text-ink-400">{p.summary}</span>
                  <span className="ml-auto text-ink-500">
                    {t("tsBy", { ts: formatTs(p.ts), operator: p.operator })}
                  </span>
                </div>
                <div className="mt-1.5 flex items-center gap-2 text-ink-400">
                  <span>{t("fieldChanges", { count: fieldCount })}</span>
                  <button
                    onClick={() => setExpanded(isOpen ? null : p.id)}
                    className="btn-ghost text-[11px] py-0 px-1"
                  >
                    {isOpen ? t("hideDiff") : t("showDiff")}
                  </button>
                  <span className="ml-auto flex gap-1.5">
                    <button
                      onClick={() => void reject(p)}
                      disabled={busy === `${p.id}:reject`}
                      className="btn-ghost text-[11px] py-0.5 text-[#ef4560]"
                    >
                      {busy === `${p.id}:reject` ? "…" : t("reject")}
                    </button>
                    <button
                      onClick={() => void approve(p)}
                      disabled={busy === `${p.id}:apply`}
                      className="btn-ghost text-[11px] py-0.5 text-accent-300"
                    >
                      {busy === `${p.id}:apply` ? t("applying") : t("approveApply")}
                    </button>
                  </span>
                </div>
                {isOpen ? (
                  <div className="embedded-table-scroll mt-2 max-h-64">
                    <table className="table w-full text-[11px]">
                      <thead>
                        <tr className="text-ink-400">
                          <th>{t("colField")}</th>
                          <th>{t("colBefore")}</th>
                          <th>{t("colAfter")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(p.diff || {}).map(([field, change]) => (
                          <tr key={field}>
                            <td className="text-brand-200">{field}</td>
                            <td className="max-w-xs align-top">
                              <DiffValue value={change.before} tone="before" />
                            </td>
                            <td className="max-w-xs align-top">
                              <DiffValue value={change.after} tone="after" />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
