"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ActionMenu } from "../../components/ActionMenu";
import { FilterBar, Pagination, SearchField, TableViewport, useListPage } from "../../components/ListControls";
import { ModePill } from "../../components/ModePill";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Kpi,
  LoadingState,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { AccountProposalsCard } from "../../components/accounts/AccountProposalsCard";
import { AddAccountForm } from "../../components/accounts/AddAccountForm";
import { ExchangeAuthorWizard } from "../../components/accounts/ExchangeAuthorWizard";
import { WalletProviderPanel } from "../../components/accounts/WalletProviderPanel";
import { clientApi } from "../../lib/clientApi";
import type { AccountSummary } from "../../lib/clientApi";
import { formatBalance } from "../../lib/currentAccount";
import { confirm as confirmDialog, prompt as promptDialog } from "../../lib/dialogs";

function money(value: unknown, currency: string = "USDT"): string {
  return formatBalance(value, currency);
}

function modePill(
  mode: string,
): "ok" | "warn" | "danger" | "brand" | "neutral" {
  switch (mode) {
    case "live":
      return "danger";
    case "canary":
      return "warn";
    case "shadow":
      return "brand";
    case "paper":
      return "ok";
    default:
      return "neutral";
  }
}

function statusPill(
  status: string,
): "ok" | "warn" | "danger" | "neutral" {
  switch (status) {
    case "active":
      return "ok";
    case "read_only":
      return "warn";
    case "quarantined":
    case "disabled":
      return "danger";
    default:
      return "neutral";
  }
}

// Raw status -> accountsPage.* translation key. Unknown statuses fall
// back to the raw string. Account mode stays untranslated on purpose:
// PAPER/LIVE are trading terms operators scan for in either locale
// (see ModePill.tsx).
const ACCOUNT_STATUS_KEYS: Record<string, string> = {
  active: "statusActive",
  read_only: "statusReadOnly",
  disabled: "statusDisabled",
  quarantined: "statusQuarantined",
};

function enumLabel(
  map: Record<string, string>,
  value: string,
  t: (key: string) => string,
): string {
  const key = map[value] ?? map[value.toLowerCase()];
  return key ? t(key) : value;
}

export default function AccountsPage() {
  const t = useTranslations("accounts");
  const tEnum = useTranslations("accountsPage");
  const zh = useLocale().startsWith("zh");
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showWizard, setShowWizard] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState("all");
  const generation = useRef(0);

  async function load() {
    const request = ++generation.current;
    setLoading(true);
    try {
      const res = await clientApi.accountsList();
      if (request !== generation.current) return;
      setAccounts(res.accounts || []);
      setLoadError(null);
    } catch (e) {
      if (request === generation.current) setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => { clearInterval(t); generation.current += 1; };
  }, []);

  async function quarantine(
    account_id: string,
    next: "active" | "quarantined" | "disabled" | "read_only",
  ) {
    setBusy(`${account_id}:${next}`);
    try {
      const reason =
        next !== "active"
          ? await promptDialog({
              message: t("reasonPrompt", { id: account_id, status: next }),
              defaultValue: t("manual"),
            })
          : undefined;
      if (next !== "active" && reason == null) {
        setBusy(null);
        return;
      }
      const res = await clientApi.accountsQuarantine({
        account_id,
        status: next,
        reason: reason || undefined,
        operator: "dashboard",
      });
      if (!res.ok) throw new Error(res.detail || res.error || "failed");
      if (res.account) {
        setAccounts((prev) =>
          prev.map((a) =>
            a.profile.id === account_id ? res.account! : a,
          ),
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function resetPaper(acc: AccountSummary) {
    const aid = acc.profile.id;
    if (acc.profile.mode !== "paper") return;
    const currentBalance = Number(acc.profile.initial_balance_usd) || 0;
    const raw = await promptDialog({
      message: t("resetPrompt", { id: aid }),
      defaultValue: String(currentBalance || ""),
    });
    if (raw == null) return;
    const trimmed = raw.trim();
    let initial: number | undefined;
    if (trimmed !== "") {
      const parsed = Number(trimmed);
      if (!Number.isFinite(parsed) || parsed < 0) {
        setError(t("invalidBalance", { value: trimmed }));
        return;
      }
      initial = parsed;
    }
    setBusy(`${aid}:reset`);
    try {
      let res = await clientApi.accountsResetPaper({
        account_id: aid,
        initial_balance_usd: initial,
        operator: "dashboard",
      });
      if (!res.ok && res.error === "account_busy" && res.state) {
        const proceed = await confirmDialog({
          message: t("accountBusy", {
            id: aid,
            orders: res.state.active_orders,
            positions: res.state.open_positions,
            executors: res.state.active_executors,
          }),
          tone: "danger",
        });
        if (!proceed) {
          setBusy(null);
          return;
        }
        res = await clientApi.accountsResetPaper({
          account_id: aid,
          initial_balance_usd: initial,
          force: true,
          operator: "dashboard",
        });
      }
      if (!res.ok) throw new Error(res.detail || res.error || "failed");
      if (res.account) {
        setAccounts((prev) =>
          prev.map((a) => (a.profile.id === aid ? res.account! : a)),
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const totals = accounts.reduce(
    (acc, a) => {
      acc.total += 1;
      if (a.profile.live_trading_enabled) acc.live += 1;
      if (a.profile.status === "quarantined") acc.quarantined += 1;
      acc.executors += a.active_executors.length;
      acc.positions += a.open_position_count;
      acc.protections += a.protection_count;
      acc.reserved += Number(a.reserved_usd || 0);
      acc.currencies.add(a.profile.base_currency || "USDT");
      return acc;
    },
    {
      total: 0,
      live: 0,
      quarantined: 0,
      executors: 0,
      positions: 0,
      protections: 0,
      reserved: 0,
      currencies: new Set<string>(),
    },
  );

  const filtered = useMemo(() => accounts.filter(({ profile }) =>
    (mode === "all" || profile.mode === mode) && `${profile.id} ${profile.venue} ${profile.wallet_id || ""} ${profile.status}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [accounts, mode, query]);
  const paging = useListPage(filtered, `${query}:${mode}`);

  return (
    <div>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => setShowWizard((s) => !s)}
              className="btn-ghost text-xs"
            >
              {showWizard ? t("closeWizard") : t("addExchange")}
            </button>
            <button
              onClick={() => setShowAdd((s) => !s)}
              className="btn-primary text-xs"
            >
              {showAdd ? t("closeForm") : t("addAccount")}
            </button>
            <button
              onClick={load}
              disabled={loading}
              className="btn-ghost text-xs"
            >
              {loading ? t("loading") : t("refresh")}
            </button>
          </div>
        }
      />
      <SectionTabs section="trading" />
      <PageBody>
        {error && <ErrorBanner error={error} />}
        <ErrorBanner error={loadError} onRetry={() => void load()} />

        <div className="flex flex-wrap items-end gap-x-8 gap-y-3 px-1">
          <Kpi inline label={t("accountsKpi")} value={`${totals.total}`} tone="brand" />
          <Kpi
            inline
            label={t("live")}
            value={`${totals.live}`}
            tone={totals.live > 0 ? "warn" : "neutral"}
            delta={t("quarantined", { count: totals.quarantined })}
          />
          <Kpi
            inline
            label={t("openPositions")}
            value={`${totals.positions}`}
            delta={t("protectionRules", { count: totals.protections })}
          />
          <Kpi
            inline
            label={t("reservedUsd")}
            value={money(totals.reserved, "USDT")}
            delta={
              totals.currencies.size === 1
                ? t("currencyCount", { count: totals.currencies.size })
                : t("currencyCountPlural", { count: totals.currencies.size })
            }
          />
        </div>

        {showWizard ? (
          <ExchangeAuthorWizard
            onApproved={() => {
              setShowWizard(false);
            }}
          />
        ) : null}

        <Advanced
          title={t("walletPanelTitle")}
          description={t("walletPanelDesc")}
          storageKey="nerya.accounts.advanced.wallet"
        >
          <WalletProviderPanel bare onChanged={() => void load()} />
        </Advanced>

        {showAdd ? (
          <AddAccountForm
            onCancel={() => setShowAdd(false)}
            onSaved={(account) => {
              setShowAdd(false);
              setAccounts((prev) => {
                const idx = prev.findIndex(
                  (a) => a.profile.id === account.profile.id,
                );
                if (idx === -1) return [...prev, account];
                const copy = prev.slice();
                copy[idx] = account;
                return copy;
              });
            }}
            onProposed={() => {
              setShowAdd(false);
            }}
          />
        ) : null}

        <AccountProposalsCard onApplied={() => void load()} />

        <Card
          title={t("allAccounts")}
          description={t("allAccountsDesc")}
        >
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <SearchField value={query} onChange={setQuery} label={zh ? "搜索账户、交易所或钱包" : "Search accounts, venues or wallets"} className="w-full sm:max-w-sm" />
            <FilterBar label={zh ? "账户模式" : "Account mode"} value={mode} onChange={setMode}
              options={[{ value: "all", label: zh ? "全部" : "All" }, ...["paper", "shadow", "canary", "live"].map((value) => ({ value, label: value.toUpperCase() }))]} />
          </div>
          {loading && !accounts.length ? <LoadingState /> : loadError && !accounts.length ? null : filtered.length === 0 ? (
            <Empty
              label={query || mode !== "all" ? (zh ? "没有符合条件的账户" : "No matching accounts") : t("noAccounts")}
              action={query || mode !== "all" ? <button className="btn btn-ghost" onClick={() => { setQuery(""); setMode("all"); }}>{zh ? "清除筛选" : "Clear filters"}</button> : <button className="btn btn-primary" onClick={() => setShowAdd(true)}>{t("addAccount")}</button>}
            />
          ) : (
            <><TableViewport label={t("allAccounts")} className="max-h-[560px]">
              <table className="table w-full">
                <thead>
                  <tr className="text-[11px] text-ink-400">
                    <th>{t("colId")}</th>
                    <th>{t("colMode")}</th>
                    <th>{t("colStatus")}</th>
                    <th className="text-right">{t("colTotal")}</th>
                    <th className="text-right">{t("colReserved")}</th>
                    <th className="text-right">{t("colPositions")}</th>
                    <th><span className="sr-only">{zh ? "账户操作" : "Account actions"}</span></th>
                  </tr>
                </thead>
                <tbody>
                  {paging.rows.map((acc) => {
                    const p = acc.profile;
                    return (
                      <tr key={p.id} className="group text-xs">
                        <td>
                          <Link
                            href={`/accounts/${encodeURIComponent(p.id)}`}
                            className="font-mono text-brand-200 hover:text-brand-100"
                          >
                            {p.id}
                          </Link>
                          <div className="mt-1 text-xs text-[color:var(--text-muted)]">{p.venue} / {p.base_currency || "USDT"}</div>
                        </td>
                        <td>
                          {p.mode === "paper" || p.mode === "live" ? <ModePill mode={p.mode} /> : <Pill tone={modePill(p.mode)}>{p.mode.toUpperCase()}</Pill>}
                        </td>
                        <td>
                          <Pill tone={statusPill(p.status)}>
                            {enumLabel(ACCOUNT_STATUS_KEYS, p.status, tEnum)}
                          </Pill>
                        </td>
                        <td className="text-right font-mono tabular-nums">
                          {money(acc.snapshot?.total_usd, p.base_currency)}
                        </td>
                        <td
                          className={`text-right font-mono tabular-nums ${
                            acc.reserved_usd > 0 ? "text-warn" : ""
                          }`}
                        >
                          {money(acc.reserved_usd, p.base_currency)}
                        </td>
                        <td className="text-right font-mono tabular-nums">
                          {acc.open_position_count}
                        </td>
                        <td>
                          <ActionMenu label={`${zh ? "账户操作" : "Account actions"}: ${p.id}`} disabled={Boolean(busy)} items={[
                            p.status === "active" && { key: "quarantine", label: t("quarantineBtn"), danger: true, onSelect: () => quarantine(p.id, "quarantined") },
                            p.status === "active" && { key: "readonly", label: t("readOnlyBtn"), onSelect: () => quarantine(p.id, "read_only") },
                            p.status !== "active" && { key: "active", label: t("reactivate"), onSelect: () => quarantine(p.id, "active") },
                            p.mode === "paper" && { key: "reset", label: t("resetPaper"), danger: true, onSelect: () => resetPaper(acc) },
                          ]} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableViewport><Pagination {...paging} /></>
          )}
        </Card>
      </PageBody>
    </div>
  );
}
