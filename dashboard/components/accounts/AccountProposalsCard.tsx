"use client";

import { useEffect, useState } from "react";
import { Card, Empty, Pill } from "../Page";
import {
  clientApi,
  type AccountProposalView,
} from "../../lib/clientApi";

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
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/**
 * Pending account proposal review card (04-29 §11 P9).
 *
 * Shows account_roster_patch proposals staged via /accounts/upsert
 * with ``apply: false``. The operator can approve (writes the YAML
 * via the same vault-only path the direct upsert uses) or reject
 * the proposal without touching accounts.yml at all.
 */
export function AccountProposalsCard({ onApplied }: Props) {
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
    if (
      !window.confirm(
        `Approve proposal ${p.id}?\n\n` +
          `${p.operation} account ${p.target_id}\n` +
          `${Object.keys(p.diff || {}).length} field(s) change(s).\n\n` +
          "This writes accounts.yml.",
      )
    )
      return;
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
    const note = window.prompt(`Reject proposal ${p.id}? Reason:`, "");
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
      title={`Pending account proposals (${proposals.length})`}
      description="Each row was staged via /accounts/upsert with apply=false. Approving applies the change atomically through the vault-only upsert helper."
      actions={
        <button
          onClick={() => void load()}
          disabled={loading}
          className="btn-ghost text-xs"
        >
          {loading ? "…" : "Refresh"}
        </button>
      }
    >
      {error && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      )}
      {proposals.length === 0 ? (
        <Empty label="No pending proposals." />
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
                    {formatTs(p.ts)} · by {p.operator}
                  </span>
                </div>
                <div className="mt-1.5 flex items-center gap-2 text-ink-400">
                  <span>{fieldCount} field change(s)</span>
                  <button
                    onClick={() => setExpanded(isOpen ? null : p.id)}
                    className="btn-ghost text-[11px] py-0 px-1"
                  >
                    {isOpen ? "hide diff" : "show diff"}
                  </button>
                  <span className="ml-auto flex gap-1.5">
                    <button
                      onClick={() => void reject(p)}
                      disabled={busy === `${p.id}:reject`}
                      className="btn-ghost text-[11px] py-0.5 text-[#ef4560]"
                    >
                      {busy === `${p.id}:reject` ? "…" : "Reject"}
                    </button>
                    <button
                      onClick={() => void approve(p)}
                      disabled={busy === `${p.id}:apply`}
                      className="btn-ghost text-[11px] py-0.5 text-accent-300"
                    >
                      {busy === `${p.id}:apply` ? "Applying…" : "Approve & apply"}
                    </button>
                  </span>
                </div>
                {isOpen ? (
                  <div className="embedded-table-scroll mt-2 max-h-64">
                    <table className="table w-full font-mono text-[11px]">
                      <thead>
                        <tr className="text-ink-400">
                          <th>Field</th>
                          <th>Before</th>
                          <th>After</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(p.diff || {}).map(([field, change]) => (
                          <tr key={field}>
                            <td className="text-brand-200">{field}</td>
                            <td className="text-ink-400 max-w-xs truncate">
                              {formatValue(change.before)}
                            </td>
                            <td className="text-amber-200 max-w-xs truncate">
                              {formatValue(change.after)}
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
