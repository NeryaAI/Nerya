"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Empty } from "../Page";
import { Select } from "../Select";
import { clientApi } from "../../lib/clientApi";
import type {
  AccountCredentialField,
  AccountSummary,
  SecretRef,
  WalletBinding,
  WalletProviderInfo,
} from "../../lib/clientApi";

const MODE_OPTIONS = ["paper", "shadow", "canary", "live"] as const;
const KIND_OPTIONS = ["cex", "dex", "perp", "futures", "chain"] as const;

interface CredentialSlot {
  field: string;
  value: string;
  label?: string;
  kind?: string;
  sensitive?: boolean;
  required?: boolean;
  placeholder?: string;
  description?: string;
}

function mergeCredentialSchema(
  fields: AccountCredentialField[],
  existingSlots: CredentialSlot[],
  existingCredentials: Record<string, string>,
): CredentialSlot[] {
  if (!fields.length) return existingSlots;
  const byField = new Map(existingSlots.map((slot) => [slot.field, slot]));
  const next: CredentialSlot[] = fields.map((field) => {
    const existing = byField.get(field.name);
    return {
      field: field.name,
      value: existing?.value ?? existingCredentials[field.name] ?? "",
      label: field.label || field.name,
      kind: field.kind,
      sensitive: field.sensitive,
      required: field.required,
      placeholder: field.placeholder,
      description: field.description,
    };
  });
  const schemaNames = new Set(fields.map((field) => field.name));
  for (const slot of existingSlots) {
    if (!schemaNames.has(slot.field)) next.push(slot);
  }
  return next;
}

interface Props {
  initial?: Partial<AccountSummary["profile"]>;
  onSaved?: (account: AccountSummary) => void;
  onProposed?: (proposalId: string) => void;
  onCancel?: () => void;
}

export function AddAccountForm({
  initial,
  onSaved,
  onProposed,
  onCancel,
}: Props) {
  const t = useTranslations("addAccountForm");
  const PERMISSION_PRESETS = [
    {
      id: "read_only",
      label: t("permissionReadOnly"),
      permissions: { read_balances: true, place_order: false, cancel_order: false },
    },
    {
      id: "trade",
      label: t("permissionTradeCancel"),
      permissions: { read_balances: true, place_order: true, cancel_order: true },
    },
  ] as const;
  const [id, setId] = useState(initial?.id || "");
  const [venue, setVenue] = useState(initial?.venue || "");
  const [venueOptions, setVenueOptions] = useState<Array<{ id: string; label: string; kind?: string }>>([]);
  const [kind, setKind] = useState<string>(initial?.kind || "cex");
  const [mode, setMode] =
    useState<(typeof MODE_OPTIONS)[number]>((initial?.mode as (typeof MODE_OPTIONS)[number]) || "paper");
  const [baseCurrency, setBaseCurrency] = useState(
    initial?.base_currency || "USDT",
  );
  const [subaccount, setSubaccount] = useState(initial?.subaccount || "");
  const [initialBalance, setInitialBalance] = useState(
    initial?.initial_balance_usd ?? 10000,
  );
  const [liveTradingEnabled, setLiveTradingEnabled] = useState(
    initial?.live_trading_enabled ?? false,
  );
  const [walletId, setWalletId] = useState(initial?.wallet_id || "");
  const [permissions, setPermissions] = useState({
    read_balances: initial?.permissions?.read_balances ?? true,
    place_order: initial?.permissions?.place_order ?? false,
    cancel_order: initial?.permissions?.cancel_order ?? false,
  });
  const [limits, setLimits] = useState<Record<string, number>>(() => ({
    max_account_nav_usd:
      Number(initial?.limits?.["max_account_nav_usd"]) || 10000,
    max_strategy_allocation_pct:
      Number(initial?.limits?.["max_strategy_allocation_pct"]) || 50,
    max_order_notional_usd:
      Number(initial?.limits?.["max_order_notional_usd"]) || 250,
    max_daily_loss_usd:
      Number(initial?.limits?.["max_daily_loss_usd"]) || 0,
    max_drawdown_pct: Number(initial?.limits?.["max_drawdown_pct"]) || 0,
  }));
  const [credentialSlots, setCredentialSlots] = useState<CredentialSlot[]>(
    () => {
      const out: CredentialSlot[] = [];
      const cred = initial?.credentials || {};
      for (const [k, v] of Object.entries(cred)) {
        out.push({ field: k, value: String(v), sensitive: true });
      }
      return out;
    },
  );
  const [schemaLabel, setSchemaLabel] = useState("");
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [vaultRefs, setVaultRefs] = useState<SecretRef[]>([]);
  const [bindings, setBindings] = useState<WalletBinding[]>([]);
  const [busy, setBusy] = useState(false);
  const [balanceBusy, setBalanceBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [balanceNotice, setBalanceNotice] = useState<string | null>(null);
  // Operators can stage the change as a
  // proposal that another approver clicks through. Defaults to
  // "Save now" for the legacy direct-write behaviour.
  const [submitMode, setSubmitMode] = useState<"apply" | "propose">("apply");

  useEffect(() => {
    let mounted = true;
    Promise.all([
      clientApi.secretsList().catch(() => ({ refs: [] })),
      clientApi.walletConfigured().catch(() => ({ bindings: [], count: 0 })),
      clientApi.walletProviders().catch(() => ({ providers: [] as WalletProviderInfo[] })),
      clientApi.marketVenues().catch(() => ({ venues: [] })),
      clientApi.exchangeProviders().catch(() => ({ providers: [] })),
    ]).then(([secrets, wallets, walletProviders, marketVenues, exchangeProviders]) => {
      if (!mounted) return;
      setVaultRefs(secrets.refs || []);
      setBindings(wallets.bindings || []);
      const byId = new Map<string, { id: string; label: string; kind?: string }>();
      for (const row of walletProviders.providers || []) {
        const id = String(row.id || "").trim().toLowerCase();
        if (id) byId.set(id, {
          id,
          label: row.label || id,
          kind: "chain",
        });
        for (const source of row.market_data_sources || []) {
          const alias = String(source.venue || "").trim().toLowerCase();
          if (alias && !byId.has(alias)) byId.set(alias, {
            id: alias,
            label: `${source.label || alias} → ${id}`,
            kind: "chain",
          });
        }
      }
      for (const row of marketVenues.venues || []) {
        const id = String(row.name || "").trim().toLowerCase();
        if (id) byId.set(id, { id, label: row.label || id });
      }
      for (const row of exchangeProviders.providers || []) {
        const id = String(row.id || "").trim().toLowerCase();
        if (id && !byId.has(id)) byId.set(id, {
          id,
          label: row.label || id,
          kind: row.kind,
        });
      }
      if (initial?.venue && !byId.has(initial.venue)) {
        byId.set(initial.venue, { id: initial.venue, label: initial.venue });
      }
      setVenueOptions(Array.from(byId.values()).sort((a, b) => a.id.localeCompare(b.id)));
    });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    const targetVenue = venue.trim();
    if (!targetVenue) {
      setSchemaLabel("");
      setSchemaError(null);
      return;
    }
    let mounted = true;
    setSchemaLoading(true);
    clientApi.accountsIntakeSchema({
      venue: targetVenue,
      account_kind: kind,
    })
      .then((res) => {
        if (!mounted) return;
        if (!res.ok) {
          setSchemaLabel("");
          setSchemaError(res.detail || res.error || t("errSchemaUnavailable"));
          return;
        }
        const fields = res.credential_fields || [];
        setSchemaLabel(res.provider_label || targetVenue);
        setSchemaError(null);
        setCredentialSlots((prev) =>
          mergeCredentialSchema(fields, prev, initial?.credentials || {}),
        );
      })
      .catch((e) => {
        if (!mounted) return;
        setSchemaLabel("");
        setSchemaError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (mounted) setSchemaLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [kind, venue, initial?.credentials]);

  const walletBacked = Boolean(walletId.trim()) || kind === "chain" || kind === "dex";
  const requiresCredentials = mode !== "paper" && permissions.read_balances && !walletBacked;
  const usesRealBalances = mode !== "paper";
  const editing = !!initial?.id;

  const credentialsRecord = useMemo(() => {
    const out: Record<string, string> = {};
    for (const slot of credentialSlots) {
      const field = slot.field.trim();
      const value = slot.value.trim();
      if (field && value && slot.sensitive !== false) out[field] = value;
    }
    return out;
  }, [credentialSlots]);

  const providerConfigRecord = useMemo(() => {
    const out: Record<string, string> = {};
    for (const slot of credentialSlots) {
      const field = slot.field.trim();
      const value = slot.value.trim();
      if (field && value && slot.sensitive === false) out[field] = value;
    }
    return out;
  }, [credentialSlots]);

  function addCredentialSlot() {
    setCredentialSlots((prev) => [...prev, { field: "", value: "", sensitive: true }]);
  }
  function updateCredentialSlot(idx: number, patch: Partial<CredentialSlot>) {
    setCredentialSlots((prev) =>
      prev.map((slot, i) => (i === idx ? { ...slot, ...patch } : slot)),
    );
  }
  function removeCredentialSlot(idx: number) {
    setCredentialSlots((prev) => prev.filter((_, i) => i !== idx));
  }

  async function submit() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await clientApi.accountsUpsert({
        id: id.trim(),
        venue: venue.trim(),
        kind,
        mode,
        live_trading_enabled: liveTradingEnabled,
        base_currency: baseCurrency.trim() || "USDT",
        subaccount: subaccount.trim(),
        initial_balance_usd: mode === "paper" ? Number(initialBalance) || 0 : 0,
        wallet_id: walletId.trim() || undefined,
        permissions,
        limits,
        provider_config: providerConfigRecord,
        credentials: credentialsRecord,
        operator: "dashboard",
        apply: submitMode === "apply",
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || "upsert_failed");
      }
      if (submitMode === "propose") {
        if (!res.proposal) {
          throw new Error("missing_proposal_response");
        }
        setNotice(
          t("stagedProposalNotice", {
            id: res.proposal.id,
            target: res.proposal.target_id,
            operation: res.proposal.operation,
          }),
        );
        onProposed?.(res.proposal.id);
      } else {
        if (!res.account) {
          throw new Error("missing_account_response");
        }
        setNotice(t("savedNotice", { id: res.account.profile.id }));
        onSaved?.(res.account);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function testBalances() {
    setBalanceBusy(true);
    setError(null);
    setBalanceNotice(null);
    try {
      const res = await clientApi.accountsTestBalance({
        id: id.trim() || undefined,
        venue: venue.trim(),
        kind,
        mode,
        live_trading_enabled: liveTradingEnabled,
        base_currency: baseCurrency.trim() || "USDT",
        wallet_id: walletId.trim() || undefined,
        permissions,
        limits,
        provider_config: providerConfigRecord,
        credentials: credentialsRecord,
        initial_balance_usd: mode === "paper" ? Number(initialBalance) || 0 : 0,
      });
      if (!res.ok || !res.snapshot) {
        throw new Error(res.detail || res.error || "balance_test_failed");
      }
      const snap = res.snapshot;
      const nav = Number(snap.total_usd ?? snap.nav_usd ?? 0);
      const health = String(snap.health || "unknown");
      setBalanceNotice(t("balanceTestOk", {
        health,
        nav: nav.toFixed(2),
      }));
    } catch (e) {
      setBalanceNotice(
        t("balanceTestFailed", {
          detail: e instanceof Error ? e.message : String(e),
        }),
      );
    } finally {
      setBalanceBusy(false);
    }
  }

  return (
    <Card
      title={editing ? t("editTitle", { id: initial?.id ?? "" }) : t("addTitle")}
      description={t("description")}
      actions={
        <div className="flex gap-2 items-center">
          <div className="min-w-[160px]">
            <Select<"apply" | "propose">
              value={submitMode}
              onChange={(value) => setSubmitMode(value)}
              disabled={busy}
              options={[
                { value: "apply", label: t("applyNow") },
                { value: "propose", label: t("proposeForReview") },
              ]}
              size="sm"
              ariaLabel={t("submitModeTitle")}
            />
          </div>
          {onCancel ? (
            <button onClick={onCancel} className="btn-ghost text-xs">
              {t("cancel")}
            </button>
          ) : null}
          <button
            onClick={testBalances}
            disabled={balanceBusy || !venue || (!id && !walletId)}
            className="btn-ghost text-xs"
          >
            {balanceBusy ? t("testingBalance") : t("testBalance")}
          </button>
          <button
            onClick={submit}
            disabled={busy || !id || !venue}
            className="btn-ghost text-xs text-accent-300"
          >
            {busy
              ? submitMode === "propose"
                ? t("staging")
                : t("saving")
              : submitMode === "propose"
              ? t("stageProposal")
              : editing
              ? t("save")
              : t("create")}
          </button>
        </div>
      }
    >
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 text-sm">
        <div className="space-y-3">
          <Field label={t("fieldAccountId")}>
            <input
              value={id}
              onChange={(e) => setId(e.target.value)}
              disabled={editing}
              placeholder="bn-live-spot"
              className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
            />
          </Field>
          <Field label={t("fieldVenue")}>
            <Select
              value={venue}
              onChange={(value) => {
                setVenue(value);
                const option = venueOptions.find((row) => row.id === value);
                if (option?.kind === "chain") setKind("chain");
              }}
              options={[
                { value: "", label: t("selectVenue") },
                ...venueOptions.map((option) => ({
                  value: option.id,
                  label: `${option.label} (${option.id})`,
                })),
              ]}
              size="sm"
              ariaLabel={t("fieldVenue")}
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label={t("fieldKind")}>
              <Select
                value={kind}
                onChange={(value) => setKind(value)}
                options={KIND_OPTIONS.map((k) => ({ value: k, label: k }))}
                size="sm"
                ariaLabel={t("fieldKind")}
              />
            </Field>
            <Field label={t("fieldMode")}>
              <Select<(typeof MODE_OPTIONS)[number]>
                value={mode}
                onChange={(value) => setMode(value)}
                options={MODE_OPTIONS.map((m) => ({ value: m, label: m }))}
                size="sm"
                ariaLabel={t("fieldMode")}
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label={t("fieldBaseCurrency")}>
              <input
                list="account-currency-suggestions"
                value={baseCurrency}
                onChange={(e) =>
                  setBaseCurrency(e.target.value.toUpperCase())
                }
                className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                placeholder="USDT / USDC / CNY / JPY / HKD …"
                title={t("baseCurrencyTitle")}
              />
              <datalist id="account-currency-suggestions">
                <option value="USDT" />
                <option value="USDC" />
                <option value="USD" />
                <option value="CNY" />
                <option value="HKD" />
                <option value="JPY" />
                <option value="EUR" />
                <option value="GBP" />
                <option value="KRW" />
                <option value="BTC" />
              </datalist>
            </Field>
            <Field label={t("fieldSubaccount")}>
              <input
                value={subaccount}
                onChange={(e) => setSubaccount(e.target.value)}
                placeholder={t("optional")}
                className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-2">
            {usesRealBalances ? (
              <Field label={t("fieldBalanceSource")}>
                <div className="rounded border border-brand-500/15 bg-ink-950/30 px-2 py-1.5 text-[11px] text-ink-400">
                  {t("realBalanceHint")}
                </div>
              </Field>
            ) : (
              <Field label={t("fieldInitialBalance")}>
                <input
                  type="number"
                  value={initialBalance}
                  onChange={(e) => setInitialBalance(Number(e.target.value))}
                  className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                />
              </Field>
            )}
            <Field label={t("fieldLiveTrading")}>
              <label className="flex items-center gap-2 mt-1.5 text-xs text-ink-300">
                <input
                  type="checkbox"
                  checked={liveTradingEnabled}
                  onChange={(e) =>
                    setLiveTradingEnabled(e.target.checked)
                  }
                />
                live_trading_enabled
              </label>
              {requiresCredentials && !liveTradingEnabled ? (
                <div className="text-[11px] text-[#f5a524] mt-0.5">
                  {t("canaryLiveNeeds")}
                </div>
              ) : null}
            </Field>
          </div>

          <Field label={t("fieldWalletBinding")}>
            <Select
              value={walletId}
              onChange={(value) => setWalletId(value)}
              options={[
                { value: "", label: t("noneUseDefault") },
                ...bindings.map((b) => ({
                  value: b.wallet_id,
                  label:
                    `${b.wallet_id} · ${b.provider}` +
                    (b.source === "legacy" ? ` (${t("defaultTag")})` : ""),
                })),
              ]}
              size="sm"
              ariaLabel={t("fieldWalletBinding")}
            />
          </Field>
        </div>

        <div className="space-y-3">
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <div className="text-ink-400 text-xs">{t("permissions")}</div>
              <div className="min-w-[160px]">
                <Select
                  value={
                    permissions.place_order || permissions.cancel_order
                      ? "trade"
                      : "read_only"
                  }
                  onChange={(value) => {
                    const preset = PERMISSION_PRESETS.find((p) => p.id === value);
                    if (preset) setPermissions(preset.permissions);
                  }}
                  options={PERMISSION_PRESETS.map((preset) => ({
                    value: preset.id,
                    label: preset.label,
                  }))}
                  size="sm"
                  ariaLabel={t("permissions")}
                />
              </div>
            </div>
            <div className="space-y-1 text-xs">
              {(
                ["read_balances", "place_order", "cancel_order"] as const
              ).map((k) => (
                <label key={k} className="flex items-center gap-2 text-ink-200">
                  <input
                    type="checkbox"
                    checked={permissions[k]}
                    onChange={(e) =>
                      setPermissions((prev) => ({
                        ...prev,
                        [k]: e.target.checked,
                      }))
                    }
                  />
                  {k}
                </label>
              ))}
              <div className="text-ink-500 text-[11px] mt-1">
                ⚠ <span className="font-mono">withdraw</span> {t("withdrawPinned")}
              </div>
            </div>
          </div>

          <div>
            <div className="text-ink-400 text-xs mb-1">{t("limits")}</div>
            <div className="space-y-1 text-xs font-mono">
              {Object.keys(limits).map((k) => (
                <label
                  key={k}
                  className="flex items-center justify-between gap-2"
                >
                  <span className="text-ink-300">{k}</span>
                  <input
                    type="number"
                    value={limits[k]}
                    onChange={(e) =>
                      setLimits((prev) => ({
                        ...prev,
                        [k]: Number(e.target.value),
                      }))
                    }
                    className="w-32 bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 text-right"
                  />
                </label>
              ))}
            </div>
          </div>

          <div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-ink-400">
                {t("connectionFields")}
                {schemaLabel ? (
                  <span className="ml-1 text-ink-500">({schemaLabel})</span>
                ) : null}
              </span>
              <button
                onClick={addCredentialSlot}
                className="btn-ghost text-[11px] py-0.5"
              >
                {t("addSlot")}
              </button>
            </div>
            {schemaLoading ? (
              <div className="text-[11px] text-ink-500 mt-1">
                {t("loadingProviderFields")}
              </div>
            ) : null}
            {schemaError ? (
              <div className="text-[11px] text-[#f5a524] mt-1">
                {schemaError}. {t("canAddManualSlots")}
              </div>
            ) : null}
            {credentialSlots.length === 0 ? (
              <Empty
                label={
                  requiresCredentials
                    ? t("liveRequiresCred")
                    : t("paperCanEmpty")
                }
              />
            ) : (
              <div className="space-y-1.5">
                {credentialSlots.map((slot, idx) => (
                  <div key={`${slot.field}-${idx}`} className="space-y-1 rounded border border-brand-500/10 bg-ink-950/20 p-2 text-xs">
                    {slot.description ? (
                      <div className="text-[11px] text-ink-500">{slot.description}</div>
                    ) : null}
                    <div className="flex gap-1.5">
                    <input
                      placeholder="api_key"
                      value={slot.field}
                      onChange={(e) =>
                        updateCredentialSlot(idx, { field: e.target.value })
                      }
                      readOnly={Boolean(slot.label)}
                      className="w-1/3 bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                    />
                    <input
                      value={slot.value}
                      onChange={(e) =>
                        updateCredentialSlot(idx, { value: e.target.value })
                      }
                      type={
                        slot.sensitive !== false && !slot.value.startsWith("vault://")
                          ? "password"
                          : slot.kind === "url"
                          ? "url"
                          : "text"
                      }
                      list={slot.sensitive !== false ? "account-vault-ref-suggestions" : undefined}
                      placeholder={
                        slot.placeholder ||
                        (slot.sensitive === false ? t("publicConfigValue") : t("pastApiOrVault"))
                      }
                      className="flex-1 bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-200"
                    />
                    <button
                      onClick={() => removeCredentialSlot(idx)}
                      disabled={slot.required}
                      className="btn-ghost text-[11px] py-0.5"
                      title={slot.required ? t("requiredBySchema") : t("removeField")}
                    >
                      ×
                    </button>
                    </div>
                  </div>
                ))}
                <datalist id="account-vault-ref-suggestions">
                  {vaultRefs.map((ref) => (
                    <option key={ref.name} value={ref.ref}>
                      {ref.ref} ({ref.kind})
                    </option>
                  ))}
                </datalist>
              </div>
            )}
            {vaultRefs.length === 0 ? (
              <div className="text-[11px] text-ink-500 mt-1">
                {t("noVaultRefs")}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {error ? (
        <div className="mt-3 text-[#ef4560] text-xs font-mono">{error}</div>
      ) : null}
      {notice ? (
        <div className="mt-3 text-accent-300 text-xs">{notice}</div>
      ) : null}
      {balanceNotice ? (
        <div className="mt-3 text-ink-300 text-xs font-mono">{balanceNotice}</div>
      ) : null}
    </Card>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-ink-400 text-xs mb-1">{label}</div>
      {children}
    </div>
  );
}
