"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi, type SdkAccountConnection } from "../../lib/clientApi";
import { Advanced, Empty, ErrorBanner, LoadingState, Pill, Section } from "../Page";
import { formatTsShort } from "../../lib/format";

function amount(value: string): string {
  const [whole, fractional] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fractional ? `${grouped}.${fractional}` : grouped;
}

function money(value: string | null | undefined): string {
  if (value == null) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

export function SdkAccountsPortfolio({ refreshVersion = 0 }: { refreshVersion?: number }) {
  const t = useTranslations("sdkAccounts");
  const [connections, setConnections] = useState<SdkAccountConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  function reason(code: string | null | undefined): string {
    if (!code) return t("connectionIssue");
    if (/SecretAccessDenied|SecretNotFound|credential_required/.test(code)) return t("reason.credentials");
    if (code.includes("auth_required")) return t("reason.login");
    if (code.includes("rate_limited")) return t("reason.rateLimit");
    if (code.includes("wrong_network")) return t("reason.network");
    if (code.includes("sdk_missing") || code.includes("module_missing")) return t("reason.components");
    if (code.includes("unsupported_operation")) return t("reason.unsupported");
    if (code.includes("signing_secret")) return t("reason.readCredential");
    if (/invalid_|mismatch|required|collection_state/.test(code)) return t("reason.format");
    return t("connectionIssue");
  }

  useEffect(() => {
    let active = true;
    clientApi.sdkAccounts().then(result => {
      if (!active) return;
      if (!result.ok) throw new Error(t("loadFailed"));
      setConnections(result.connectors); setError(null);
    }).catch(() => { if (active) setError(t("loadFailed")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [refreshVersion, t]);

  async function refresh(id: string) {
    setBusy(id); setError(null);
    try {
      const result = await clientApi.sdkAccountRefresh(id);
      // Always re-read cached state: a failed collection retains old data and
      // marks it stale; displaying an old "fresh" badge would conceal failure.
      const state = await clientApi.sdkAccounts();
      if (state.ok) setConnections(state.connectors);
      if (!result.ok) setError(t("refreshFailed", { reason: reason(result.error) }));
    } catch { setError(t("refreshFailed", { reason: t("connectionIssue") })); }
    finally { setBusy(null); }
  }

  return <Section title={t("title")} description={t("description")}>
    <div data-testid="sdk-accounts-portfolio" className="min-w-0">
      <ErrorBanner error={error} />
      {loading ? <LoadingState rows={2} /> : !connections.length && !error ? <Empty
        title={t("emptyTitle")} subtitle={t("emptyDescription")}
        action={<Link href="/chat" className="btn-primary text-xs">{t("connectWithAgent")}</Link>} /> : null}
      {connections.map(connection => {
        const observation = connection.observation;
        const status = connection.stale ? "stale" : connection.collection_error ? "error"
          : connection.phase === "ready" ? observation ? "observed" : "awaitingData" : connection.phase;
        return <details key={connection.connector_id} open={connections.length <= 2}
          data-testid={`sdk-account-${connection.connector_id}`} className="min-w-0 border-b border-[color:var(--line)] py-4 last:border-b-0">
          <summary className="flex cursor-pointer flex-wrap items-center gap-3 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-400">
            <span className="min-w-0 flex-1 break-words font-medium">{connection.label}</span>
            <Pill tone={status === "stale" || status === "error" ? "warn" : status === "observed" ? "ok" : "neutral"}>
              {t(`status.${status}`)}
            </Pill>
            {observation ? <span className="text-xs text-[color:var(--text-muted)]">{formatTsShort(observation.as_of)}</span> : null}
          </summary>
          <div className="mt-4 min-w-0 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="max-w-2xl text-xs text-[color:var(--text-muted)]">{t("fundsNote")}</p>
              <button className="btn-ghost text-xs" disabled={!!busy || connection.phase !== "ready"}
                onClick={() => refresh(connection.connector_id)}>{busy === connection.connector_id ? t("updating") : t("updateAssets")}</button>
            </div>
            {connection.error || connection.collection_error ? <p role="status" className="text-xs text-warn">
              {t("needsAttention", { reason: reason(connection.collection_error || connection.error) })}</p> : null}
            {observation ? <>
              <dl className="grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-4">
                {([
                  ["pricedBalances", observation.summary.priced_balance_value_usd],
                  ["realizedPnl", observation.performance.realized_pnl_usd],
                  ["unrealizedPnl", observation.performance.unrealized_pnl_usd],
                  ["income", observation.performance.income_usd],
                ] as const).map(([label, value]) => <div key={label}>
                  <dt className="text-xs text-[color:var(--text-muted)]">{t(label)}</dt>
                  <dd title={value ?? undefined} className="mt-1 font-mono text-sm tabular-nums">{money(value)}</dd>
                </div>)}
              </dl>
              {observation.performance.period_start && observation.performance.period_end ? <p className="text-xs text-[color:var(--text-muted)]">
                {t("period", { start: formatTsShort(Number(observation.performance.period_start)), end: formatTsShort(Number(observation.performance.period_end)) })}
              </p> : null}
              {observation.performance.fees_usd != null ? <p className="text-xs text-[color:var(--text-muted)]">
                {t("periodFees", { amount: money(observation.performance.fees_usd) })}</p> : null}
              {observation.summary.unpriced_balances > 0 ? <p className="text-xs text-[color:var(--text-muted)]">
                {t("unpriced", { count: observation.summary.unpriced_balances })}</p> : null}
              <div className="max-w-full overflow-x-auto">
                <table className="table w-full text-xs" aria-label={t("balances")}>
                  <thead><tr><th>{t("token")}</th><th>{t("network")}</th><th className="text-right">{t("amount")}</th>
                    <th className="text-right">{t("available")}</th><th className="text-right">{t("value")}</th></tr></thead>
                  <tbody>{observation.balances.map(balance => <tr key={balance.asset_id}>
                    <td title={balance.token}>{balance.symbol}</td><td>{balance.chain || "—"}</td>
                    <td title={balance.amount} className="text-right font-mono whitespace-nowrap">{amount(balance.amount)}</td>
                    <td className="text-right font-mono whitespace-nowrap">{balance.available == null ? "—" : amount(balance.available)}</td>
                    <td className="text-right font-mono whitespace-nowrap">{money(balance.value_usd)}</td>
                  </tr>)}</tbody>
                </table>
              </div>
              {observation.positions.length ? <div className="max-w-full overflow-x-auto">
                <table className="table w-full text-xs" aria-label={t("positions")}>
                  <thead><tr><th>{t("position")}</th><th>{t("protocol")}</th><th className="text-right">{t("value")}</th>
                    <th className="text-right">{t("unrealizedPnl")}</th><th className="text-right">{t("rewards")}</th></tr></thead>
                  <tbody>{observation.positions.map(position => <tr key={position.id}>
                    <td>{position.label}</td><td>{position.protocol || position.kind}</td>
                    <td className="text-right font-mono">{money(position.value_usd)}</td>
                    <td className="text-right font-mono">{money(position.unrealized_pnl_usd)}</td>
                    <td className="text-right font-mono">{money(position.claimable_rewards_usd)}</td>
                  </tr>)}</tbody>
                </table>
              </div> : null}
              <Advanced title={t("connectionDetails")}>
                <dl className="space-y-2 text-xs text-[color:var(--text-muted)]">
                  <div><dt>{t("source")}</dt><dd className="break-all">{connection.skill_id} / {connection.script}</dd></div>
                  <div><dt>{t("revision")}</dt><dd className="break-all font-mono">{observation.source.revision}</dd></div>
                  <div><dt>{t("components")}</dt><dd className="break-all">{connection.requirements.join(", ") || t("noExtraSdk")}</dd></div>
                  {connection.error || connection.collection_error ? <div><dt>{t("diagnostic")}</dt><dd className="break-all font-mono">{connection.collection_error || connection.error}</dd></div> : null}
                </dl>
              </Advanced>
            </> : <p className="text-sm text-[color:var(--text-muted)]">{t("waitingDescription")}</p>}
          </div>
        </details>;
      })}
    </div>
  </Section>;
}
