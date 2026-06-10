"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Card, Empty, Pill } from "../Page";
import { clientApi } from "../../lib/clientApi";
import type {
  AccountCredentialField,
  WalletBinding,
  WalletProviderInfo,
} from "../../lib/clientApi";

function readinessTone(ready: boolean): "ok" | "warn" {
  return ready ? "ok" : "warn";
}

function capabilityTone(status?: string): "ok" | "warn" | "neutral" {
  if (status === "real") return "ok";
  if (status === "partial" || status === "experimental") return "warn";
  return "neutral";
}

export function WalletProviderPanel({
  onChanged,
  bare = false,
}: {
  onChanged?: () => void;
  bare?: boolean;
}) {
  const t = useTranslations("walletSetup");
  const [providers, setProviders] = useState<WalletProviderInfo[]>([]);
  const [bindings, setBindings] = useState<WalletBinding[]>([]);
  const [selected, setSelected] = useState<WalletProviderInfo | null>(null);
  const [fields, setFields] = useState<AccountCredentialField[]>([]);
  const [advancedFields, setAdvancedFields] = useState<AccountCredentialField[]>([]);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [manualConfigOpen, setManualConfigOpen] = useState(false);
  const [advancedConfigOpen, setAdvancedConfigOpen] = useState(false);
  const [walletId, setWalletId] = useState("");
  const [label, setLabel] = useState("");
  const [activate, setActivate] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [authEmail, setAuthEmail] = useState("");
  const [authOtp, setAuthOtp] = useState("");
  const [authQrId, setAuthQrId] = useState("");
  const [authResult, setAuthResult] = useState<Record<string, unknown> | null>(null);
  // Wallet -> account auto-create knobs. Default
  // ``live`` matches the operator picking "yes, give me a real
  // account immediately"; the backend still creates the account with
  // ``permissions.place_order = false`` so a fresh live account is
  // read-only until the operator explicitly enables trading on the
  // /accounts/<id> page. Operators uneasy with that can flip to
  // ``paper`` / ``shadow`` here before saving.
  const [autoCreateAccount, setAutoCreateAccount] = useState(true);
  const [accountMode, setAccountMode] = useState<
    "paper" | "shadow" | "canary" | "live"
  >("live");
  const [accountIdHint, setAccountIdHint] = useState("");
  const [initialBalance, setInitialBalance] = useState("");
  const [lastAccountId, setLastAccountId] = useState("");
  const [balanceTestNotice, setBalanceTestNotice] = useState<string | null>(null);

  async function load() {
    setError(null);
    const [providerRes, bindingRes] = await Promise.all([
      clientApi.walletProviders(),
      clientApi.walletConfigured().catch(() => ({ bindings: [], count: 0 })),
    ]);
    setProviders(providerRes.providers || []);
    setBindings(bindingRes.bindings || []);
  }

  useEffect(() => {
    void load().catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const bindingByProvider = useMemo(() => {
    const map = new Map<string, WalletBinding[]>();
    for (const binding of bindings) {
      const rows = map.get(binding.provider) || [];
      rows.push(binding);
      map.set(binding.provider, rows);
    }
    return map;
  }, [bindings]);

  const fieldsForSave = useMemo(
    () => [...fields, ...advancedFields],
    [fields, advancedFields],
  );
  const authJson = useMemo(() => {
    const auth = authResult?.auth;
    if (auth && typeof auth === "object") {
      const json = (auth as Record<string, unknown>).json;
      return json && typeof json === "object" ? json : {};
    }
    return {};
  }, [authResult]);
  const authLoginUrl = useMemo(
    () => firstString(authJson, ["urlForWeb", "loginUrl", "verificationUrl", "url"]),
    [authJson],
  );
  const authPairingCode = useMemo(
    () => firstString(authJson, ["pairingCode", "userCode", "code"]),
    [authJson],
  );
  const authDeviceCode = useMemo(
    () => firstString(authJson, ["deviceCode", "device_code"]),
    [authJson],
  );
  const authResultQrCodeId = useMemo(
    () => firstString(authJson, ["qrCodeId"]),
    [authJson],
  );
  const selectedAuthFlows = useMemo(
    () => visibleAuthFlows(selected),
    [selected],
  );

  function visibleAuthFlows(provider?: WalletProviderInfo | null) {
    return (provider?.auth_flows || []).filter(
      (flow) => flow.kind !== "advanced_api_key",
    );
  }

  function authKindLabel(kind?: string) {
    if (!kind) return t("authKind.unknown");
    try {
      return t(`authKind.${kind}`);
    } catch {
      return kind;
    }
  }

  function providerNeedsEmail(provider: WalletProviderInfo) {
    return Boolean(
      visibleAuthFlows(provider).some((flow) =>
        flow.kind === "email_otp" || flow.kind === "email_popup" || flow.kind === "email_magic_link",
      ),
    );
  }

  function providerHasQr(provider: WalletProviderInfo) {
    return Boolean(visibleAuthFlows(provider).some((flow) => flow.kind === "app_qr"));
  }

  function providerUsesDeviceCode(provider: WalletProviderInfo) {
    return Boolean(visibleAuthFlows(provider).some((flow) => flow.kind === "device_code"));
  }

  function providerUsesPopupLogin(provider: WalletProviderInfo) {
    return provider.id === "coinbase" || Boolean(
      visibleAuthFlows(provider).some((flow) =>
        flow.kind === "wallet_popup" || flow.kind === "email_popup",
      ),
    );
  }

  function providerAuthInstalled(provider: WalletProviderInfo) {
    const installKind = provider.auth_install_state?.kind;
    if (installKind && installKind !== "unknown" && installKind !== "noop") {
      return Boolean(provider.auth_install_state?.installed);
    }
    return Boolean(
      provider.installed ||
      provider.readiness?.installed ||
      provider.readiness?.ready,
    );
  }

  function providerUsesLogin(provider: WalletProviderInfo) {
    return providerNeedsEmail(provider) || providerHasQr(provider) || providerUsesPopupLogin(provider) || providerUsesDeviceCode(provider);
  }

  function authAlreadyStarted(provider: WalletProviderInfo) {
    if (!authResult || selected?.id !== provider.id) return false;
    const action = authResult.next_action;
    return action === "otp" || action === "qr_approval" || action === "wallet_popup_login" || action === "device_approval";
  }

  function authActionLabel(provider: WalletProviderInfo) {
    const installed = providerAuthInstalled(provider);
    const started = authAlreadyStarted(provider);
    if (providerUsesPopupLogin(provider)) {
      if (started) return t("reopenPopupLogin");
      return installed ? t("openPopupLogin") : t("installAndOpenPopupLogin");
    }
    if (providerNeedsEmail(provider)) {
      if (started) return t("resendOtp");
      return installed ? t("sendOtp") : t("installAndSendOtp");
    }
    if (providerHasQr(provider)) {
      if (started) return t("reopenLogin");
      return installed ? t("openLogin") : t("installAndOpenLogin");
    }
    if (providerUsesDeviceCode(provider)) {
      if (started) return t("reopenLogin");
      return installed ? t("openLogin") : t("installAndOpenLogin");
    }
    return installed ? t("enableWallet") : t("installAndEnableWallet");
  }

  function authBusyLabel(provider: WalletProviderInfo) {
    if (providerUsesPopupLogin(provider)) return t("openingLogin");
    if (providerNeedsEmail(provider)) return t("sendingOtp");
    if (providerHasQr(provider)) return t("openingLogin");
    if (providerUsesDeviceCode(provider)) return t("openingLogin");
    return t("enablingWallet");
  }

  function firstString(obj: unknown, keys: string[]): string {
    if (!obj || typeof obj !== "object") return "";
    const rec = obj as Record<string, unknown>;
    for (const key of keys) {
      const value = rec[key];
      if (typeof value === "string" && value.trim()) return value;
    }
    const data = rec.data;
    if (data && typeof data === "object") {
      return firstString(data, keys);
    }
    return "";
  }

  function currentWalletId(provider: WalletProviderInfo) {
    return walletId.trim() || `${provider.id}_main`;
  }

  function currentBindingLabel(provider: WalletProviderInfo) {
    const wid = currentWalletId(provider);
    return label.trim() || provider.label || wid;
  }

  function collectBindingConfig() {
    const config: Record<string, string> = {};
    for (const field of fieldsForSave) {
      const value = values[field.name]?.trim();
      if (value) config[field.name] = value;
    }
    return config;
  }

  function collectAccountAutoArgs(): {
    auto_create_account?: boolean;
    account_mode?: "paper" | "shadow" | "canary" | "live";
    account_id_hint?: string;
    initial_balance_usd?: number;
  } {
    if (!autoCreateAccount) return {};
    const trimmed = accountIdHint.trim();
    const balance = initialBalance.trim();
    const args: {
      auto_create_account: true;
      account_mode: "paper" | "shadow" | "canary" | "live";
      account_id_hint?: string;
      initial_balance_usd?: number;
    } = {
      auto_create_account: true,
      account_mode: accountMode,
    };
    if (trimmed) args.account_id_hint = trimmed;
    if (accountMode === "paper" && balance) {
      const parsed = Number(balance);
      if (Number.isFinite(parsed) && parsed >= 0) {
        args.initial_balance_usd = parsed;
      }
    }
    return args;
  }

  function accountIdFromResult(result: unknown): string {
    if (!result || typeof result !== "object") return "";
    const row = result as Record<string, unknown>;
    return typeof row.account_id === "string" ? row.account_id : "";
  }

  function defaultAccountId(provider: WalletProviderInfo) {
    return `${provider.id}_${currentWalletId(provider)}`
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "_")
      .replace(/^_+|_+$/g, "");
  }

  async function testAccountBalance() {
    if (!selected) return;
    const accountId = lastAccountId || accountIdHint.trim() || defaultAccountId(selected);
    if (!accountId) return;
    setBusy(`balance:${selected.id}`);
    setError(null);
    setBalanceTestNotice(null);
    try {
      const res = await clientApi.accountsTestBalance({ account_id: accountId });
      if (!res.ok || !res.snapshot) {
        throw new Error(res.detail || res.error || "balance_test_failed");
      }
      const snap = res.snapshot as Record<string, unknown>;
      const nav = Number(snap.total_usd ?? snap.nav_usd ?? 0);
      setBalanceTestNotice(t("balanceTestOk", {
        account: accountId,
        health: String(snap.health || "unknown"),
        nav: nav.toFixed(2),
      }));
    } catch (e) {
      setBalanceTestNotice(t("balanceTestFailed", {
        detail: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setBusy(null);
    }
  }

  function describeAccountResult(
    result: unknown,
    warning: unknown,
  ): string | null {
    if (result && typeof result === "object") {
      const row = result as Record<string, unknown>;
      const id = typeof row.account_id === "string" ? row.account_id : "";
      const mode = typeof row.mode === "string" ? row.mode : "";
      const created = Boolean(row.created);
      if (!id) return null;
      return created
        ? t("accountCreated", { id, mode })
        : t("accountLinked", { id, mode });
    }
    if (warning && typeof warning === "object") {
      const w = warning as Record<string, unknown>;
      const code = typeof w.error === "string" ? w.error : "account_create_failed";
      const detail = typeof w.detail === "string" ? w.detail : "";
      return t("accountFailed", { code, detail });
    }
    return null;
  }

  async function selectProvider(
    provider: WalletProviderInfo,
    options: { openDialog?: boolean; resetAuth?: boolean } = {},
  ) {
    const providerChanged = selected?.id !== provider.id;
    if (options.openDialog) setDialogOpen(true);
    if (options.resetAuth || providerChanged) {
      setManualConfigOpen(false);
      setAdvancedConfigOpen(false);
      setAuthResult(null);
      setAuthOtp("");
      setAuthQrId("");
      setLastAccountId("");
      setBalanceTestNotice(null);
      if (!providerNeedsEmail(provider)) setAuthEmail("");
    }
    setSelected(provider);
    setError(null);
    setNotice(null);
    setWalletId((prev) => (providerChanged || !prev ? `${provider.id}_main` : prev));
    setLabel((prev) => (providerChanged || !prev ? provider.label : prev));
    setBusy(`schema:${provider.id}`);
    try {
      const schema = await clientApi.walletCredentialSchema(provider.id);
      if (!schema.ok) throw new Error(schema.error || "schema_unavailable");
      setFields(schema.credential_fields || []);
      setAdvancedFields(schema.advanced_credential_fields || []);
      setSelected({
        ...provider,
        auth_flows: schema.auth_flows || provider.auth_flows || [],
        auth_install_state: schema.auth_install_state || provider.auth_install_state,
        credential_fields: schema.credential_fields || provider.credential_fields || [],
        advanced_credential_fields:
          schema.advanced_credential_fields || provider.advanced_credential_fields || [],
      });
      setValues((prev) => {
        const next = { ...prev };
        for (const field of [
          ...(schema.credential_fields || []),
          ...(schema.advanced_credential_fields || []),
        ]) {
          if (next[field.name] == null) next[field.name] = "";
        }
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setFields(provider.credential_fields || []);
      setAdvancedFields(provider.advanced_credential_fields || []);
    } finally {
      setBusy(null);
    }
  }

  async function install(provider: WalletProviderInfo) {
    await selectProvider(provider, { openDialog: true, resetAuth: true });
    if (providerNeedsEmail(provider)) {
      setNotice(t("emailRequired"));
      return;
    }
    await startWalletAuth(provider);
  }

  async function startWalletAuth(providerArg?: WalletProviderInfo) {
    const provider = providerArg || selected;
    if (!provider) return;
    if (providerNeedsEmail(provider) && !authEmail.trim()) {
      setError(t("emailRequired"));
      return;
    }
    setBusy(`auth:${provider.id}`);
    setError(null);
    setNotice(null);
    setAuthResult(null);
    try {
      const res = await clientApi.walletAuthStart({
        provider: provider.id,
        approve: true,
        install: !providerAuthInstalled(provider),
        email: authEmail.trim() || undefined,
        locale: typeof navigator !== "undefined" ? navigator.language || "en-US" : "en-US",
        wallet_id: currentWalletId(provider),
        label: currentBindingLabel(provider),
        config: collectBindingConfig(),
        activate,
        operator: "dashboard",
        create_binding: true,
        ...collectAccountAutoArgs(),
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || res.auth?.stderr_tail || res.auth?.stdout_tail || "auth_start_failed");
      }
      const json = res.auth?.json || {};
      const qrCodeId = firstString(json, ["qrCodeId"]);
      if (qrCodeId) setAuthQrId(qrCodeId);
      setAuthResult(res as unknown as Record<string, unknown>);
      if (res.next_action === "otp") {
        setAuthOtp("");
        setNotice(t("otpSent"));
      } else if (res.next_action === "qr_approval") {
        setNotice(t("qrReady"));
      } else if (res.next_action === "device_approval") {
        setNotice(t("deviceReady"));
      } else if (res.next_action === "wallet_popup_login") {
        setNotice(t("walletPopupReady"));
      } else {
        const accountId = accountIdFromResult(res.account);
        if (accountId) setLastAccountId(accountId);
        const baseNotice = res.bindings?.length
          ? t("walletEnabled", { wallet: currentWalletId(provider) })
          : t("installDone", { provider: provider.id });
        const acctNotice = describeAccountResult(
          res.account,
          res.account_warning,
        );
        setNotice(acctNotice ? `${baseNotice} · ${acctNotice}` : baseNotice);
        if (!providerUsesLogin(provider) && res.bindings?.length) {
          setBindings(res.bindings);
          setDialogOpen(false);
          setSelected(null);
        }
      }
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function verifyWalletAuth() {
    if (!selected) return;
    setBusy(`verify:${selected.id}`);
    setError(null);
    setNotice(null);
    try {
      const res = await clientApi.walletAuthVerify({
        provider: selected.id,
        otp: authOtp.trim() || undefined,
        deviceCode: authDeviceCode || undefined,
        qrCodeId: authQrId.trim() || undefined,
        wallet_id: currentWalletId(selected),
        label: currentBindingLabel(selected),
        config: collectBindingConfig(),
        activate,
        operator: "dashboard",
        create_binding: true,
        ...collectAccountAutoArgs(),
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || res.auth?.stderr_tail || res.auth?.stdout_tail || "auth_verify_failed");
      }
      setAuthResult(res as unknown as Record<string, unknown>);
      const acctNotice = describeAccountResult(res.account, res.account_warning);
      const accountId = accountIdFromResult(res.account);
      if (accountId) setLastAccountId(accountId);
      if (res.bindings?.length) {
        setBindings(res.bindings);
        const base = t("authVerifiedAndBound", { wallet: currentWalletId(selected) });
        setNotice(acctNotice ? `${base} · ${acctNotice}` : base);
        setDialogOpen(false);
        setSelected(null);
      } else {
        const base = t("authVerified");
        setNotice(acctNotice ? `${base} · ${acctNotice}` : base);
      }
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function refreshWalletAuthStatus() {
    if (!selected) return;
    setBusy(`status:${selected.id}`);
    setError(null);
    try {
      const res = await clientApi.walletAuthStatus(selected.id, {
        wallet_id: currentWalletId(selected),
        label: currentBindingLabel(selected),
        config: collectBindingConfig(),
        activate,
        operator: "dashboard",
        create_binding: true,
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || res.auth?.stderr_tail || res.auth?.stdout_tail || "auth_status_failed");
      }
      setAuthResult(res as unknown as Record<string, unknown>);
      if (res.bindings?.length) {
        setBindings(res.bindings);
        setNotice(t("authStatusBound", { wallet: currentWalletId(selected) }));
      } else {
        setNotice(t("authStatusLoaded"));
      }
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function saveBinding() {
    if (!selected) return;
    const wid = walletId.trim();
    if (!wid) {
      setError(t("walletIdRequired"));
      return;
    }
    setBusy(`save:${selected.id}`);
    setError(null);
    setNotice(null);
    try {
      const config: Record<string, string> = {};
      Object.assign(config, collectBindingConfig());
      const res = await clientApi.walletConfigureBinding({
        provider: selected.id,
        wallet_id: wid,
        label: label.trim() || wid,
        config,
        activate,
        operator: "dashboard",
        ...collectAccountAutoArgs(),
      });
      if (!res.ok) throw new Error(res.detail || res.error || "configure_failed");
      const acctNotice = describeAccountResult(res.account, res.account_warning);
      const accountId = accountIdFromResult(res.account);
      if (accountId) setLastAccountId(accountId);
      const base = t("storedNotice", { wallet: wid });
      setNotice(acctNotice ? `${base} · ${acctNotice}` : base);
      setBindings(res.bindings || []);
      setValues({});
      setDialogOpen(false);
      setSelected(null);
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const body = (
    <>
      <div className="space-y-3">
        {!dialogOpen && error ? (
          <div className="rounded border border-danger/35 bg-danger/10 px-3 py-2 text-xs text-rose-300">
            {error}
          </div>
        ) : null}
        {!dialogOpen && notice ? (
          <div className="rounded border border-accent-500/25 bg-accent-500/10 px-3 py-2 text-xs text-accent-200">
            {notice}
          </div>
        ) : null}

        {providers.length === 0 ? (
          <Empty label={t("noProviders")} />
        ) : (
          <div className="embedded-table-scroll">
            <table className="table w-full">
              <thead>
                <tr className="text-[11px] text-ink-400">
                  <th>{t("colProvider")}</th>
                  <th>{t("colLogin")}</th>
                  <th>{t("colReadiness")}</th>
                  <th>{t("colMarketData")}</th>
                  <th>{t("colBindings")}</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {providers.map((provider) => {
                  const ready = Boolean(provider.readiness?.ready);
                  const marketData = provider.capabilities?.market_data;
                  const providerBindings = bindingByProvider.get(provider.id) || [];
                  return (
                    <tr key={provider.id} className="text-xs align-top">
                      <td>
                        <div className="font-mono text-ink-100">{provider.id}</div>
                        <div className="text-ink-500">{provider.label}</div>
                      </td>
                      <td>
                        {visibleAuthFlows(provider).length ? (
                          <div className="flex max-w-[220px] flex-wrap gap-1">
                            {visibleAuthFlows(provider).slice(0, 3).map((flow) => (
                              <Pill key={flow.id} tone="neutral">
                                {authKindLabel(flow.kind)}
                              </Pill>
                            ))}
                          </div>
                        ) : (
                          <span className="text-ink-500">{t("noAuthFlows")}</span>
                        )}
                      </td>
                      <td>
                        <Pill tone={readinessTone(ready)}>
                          {ready ? t("ready") : t("notReady")}
                        </Pill>
                        {!ready && provider.readiness?.missing?.length ? (
                          <div className="mt-1 font-mono text-[11px] text-ink-500">
                            {provider.readiness.missing.join(", ")}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <Pill tone={capabilityTone(marketData?.status)}>
                          {marketData?.supported ? marketData.status : t("unavailable")}
                        </Pill>
                        {marketData?.note ? (
                          <div className="mt-1 text-[11px] text-ink-500">
                            {marketData.note}
                          </div>
                        ) : null}
                        {provider.market_data_sources?.length ? (
                          <div className="mt-1 font-mono text-[11px] text-ink-500">
                            {provider.market_data_sources.map((s) => s.venue).join(", ")}
                          </div>
                        ) : null}
                      </td>
                      <td className="font-mono text-ink-300">
                        {providerBindings.length
                          ? providerBindings.map((b) => b.wallet_id).join(", ")
                          : t("noBindings")}
                      </td>
                      <td className="flex flex-wrap justify-end gap-1.5">
                        <button
                          onClick={() => void install(provider)}
                          disabled={busy === `auth:${provider.id}`}
                          className="btn-ghost text-[11px] py-0.5"
                          title={provider.install_hint}
                        >
                          {busy === `auth:${provider.id}` ? authBusyLabel(provider) : authActionLabel(provider)}
                        </button>
                        <button
                          onClick={() => void selectProvider(provider, { openDialog: true })}
                          className="btn-ghost text-[11px] py-0.5 text-accent-300"
                        >
                          {t("configure")}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {selected && dialogOpen ? (
          <div
            className="fixed inset-0 z-50 flex items-start justify-center bg-black/70 px-4 py-8 backdrop-blur-sm"
            role="dialog"
            aria-modal="true"
          >
          <div className="max-h-[calc(100vh-4rem)] w-full max-w-3xl overflow-y-auto rounded border border-brand-500/25 bg-ink-950 p-4 shadow-2xl">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-ink-100">
                  {t("configFor", { provider: selected.id })}
                </div>
                <div className="text-[11px] text-ink-500">
                  {t("plaintextVaultHint")}
                </div>
              </div>
              <button
                onClick={() => {
                  setDialogOpen(false);
                  setSelected(null);
                }}
                className="btn-ghost text-[11px] py-0.5"
              >
                {t("close")}
              </button>
            </div>

            {error ? (
              <div className="mb-3 rounded border border-danger/35 bg-danger/10 px-3 py-2 text-xs text-rose-300">
                {error}
              </div>
            ) : null}
            {notice ? (
              <div className="mb-3 rounded border border-accent-500/25 bg-accent-500/10 px-3 py-2 text-xs text-accent-200">
                {notice}
              </div>
            ) : null}

            {selectedAuthFlows.length ? (
              <div className="mb-3 rounded border border-brand-500/15 bg-ink-950/40 p-3">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium text-ink-200">
                    {t("authFlows")}
                  </span>
                  {selectedAuthFlows.slice(0, 3).map((flow) => (
                    <Pill key={flow.id} tone="neutral">
                      {authKindLabel(flow.kind)}
                    </Pill>
                  ))}
                </div>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  {providerNeedsEmail(selected) ? (
                    <Field label={t("email")}>
                      <input
                        value={authEmail}
                        onChange={(e) => setAuthEmail(e.target.value)}
                        className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                        placeholder="name@example.com"
                      />
                    </Field>
                  ) : null}
                  {providerHasQr(selected) || authResultQrCodeId ? (
                    <Field label={t("qrCodeId")}>
                      <input
                        value={authQrId || authResultQrCodeId}
                        onChange={(e) => setAuthQrId(e.target.value)}
                        className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                      />
                    </Field>
                  ) : null}
                  {providerNeedsEmail(selected) ? (
                    <Field label={t("otp")}>
                      <input
                        value={authOtp}
                        onChange={(e) => setAuthOtp(e.target.value)}
                        className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                        inputMode="numeric"
                        placeholder="123456"
                      />
                    </Field>
                  ) : null}
                </div>
                {authLoginUrl || authPairingCode ? (
                  <div className="mt-3 rounded border border-accent-500/20 bg-accent-500/10 p-2 text-xs">
                    {authLoginUrl ? (
                      <a
                        href={authLoginUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="font-mono text-accent-200 underline"
                      >
                        {authLoginUrl}
                      </a>
                    ) : null}
                    {authPairingCode ? (
                      <div className="mt-1 font-mono text-ink-100">
                        {t("pairingCode")}: {authPairingCode}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    onClick={() => void startWalletAuth()}
                    disabled={busy === `auth:${selected.id}`}
                    className="btn-ghost text-xs text-accent-300"
                  >
                    {busy === `auth:${selected.id}` ? authBusyLabel(selected) : authActionLabel(selected)}
                  </button>
                  <button
                    onClick={() => void verifyWalletAuth()}
                    disabled={busy === `verify:${selected.id}`}
                    className="btn-ghost text-xs"
                  >
                    {busy === `verify:${selected.id}` ? t("verifying") : t("verifyAuth")}
                  </button>
                  <button
                    onClick={() => void refreshWalletAuthStatus()}
                    disabled={busy === `status:${selected.id}`}
                    className="btn-ghost text-xs"
                  >
                    {busy === `status:${selected.id}` ? t("loadingSchema") : t("authStatus")}
                  </button>
                </div>
              </div>
            ) : null}

            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <Field label={t("walletId")}>
                <input
                  value={walletId}
                  onChange={(e) => setWalletId(e.target.value)}
                  className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                  placeholder={`${selected.id}_main`}
                />
              </Field>
              <Field label={t("label")}>
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100"
                  placeholder={selected.label}
                />
              </Field>
            </div>

            {/*
             * Auto-create a kind=chain account
             * tied to this wallet binding. We default the checkbox
             * to ``true`` so the operator gets an account row + NAV
             * snapshot pipeline out of the box (the alternative
             * forces them to fill the /accounts form a second time
             * to mirror the wallet they just configured).
             */}
            <div className="mt-3 rounded border border-brand-500/15 bg-ink-950/30 p-3">
              <label className="flex items-start gap-2 text-xs text-ink-200">
                <input
                  type="checkbox"
                  checked={autoCreateAccount}
                  onChange={(e) => setAutoCreateAccount(e.target.checked)}
                  className="mt-0.5"
                />
                <span>
                  <div className="font-medium">{t("autoCreate.title")}</div>
                  <div className="mt-0.5 text-[11px] text-ink-500">
                    {t("autoCreate.description")}
                  </div>
                </span>
              </label>
              {autoCreateAccount ? (
                <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
                  <Field label={t("autoCreate.mode")}>
                    <select
                      value={accountMode}
                      onChange={(e) =>
                        setAccountMode(
                          e.target.value as
                            | "paper"
                            | "shadow"
                            | "canary"
                            | "live",
                        )
                      }
                      className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                    >
                      <option value="paper">{t("autoCreate.modePaper")}</option>
                      <option value="shadow">{t("autoCreate.modeShadow")}</option>
                      <option value="canary">{t("autoCreate.modeCanary")}</option>
                      <option value="live">{t("autoCreate.modeLive")}</option>
                    </select>
                  </Field>
                  <Field label={t("autoCreate.accountIdHint")}>
                    <input
                      value={accountIdHint}
                      onChange={(e) => setAccountIdHint(e.target.value)}
                      className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                      placeholder={t("autoCreate.accountIdHintPlaceholder", {
                        wallet: walletId.trim() || `${selected.id}_main`,
                      })}
                    />
                  </Field>
                  {accountMode === "paper" ? (
                    <Field label={t("autoCreate.initialBalance")}>
                      <input
                        value={initialBalance}
                        onChange={(e) => setInitialBalance(e.target.value)}
                        inputMode="decimal"
                        className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                        placeholder="0"
                      />
                    </Field>
                  ) : (
                    <Field label={t("autoCreate.balanceSource")}>
                      <div className="rounded border border-brand-500/15 bg-ink-950/30 px-2 py-1.5 text-[11px] text-ink-400">
                        {t("autoCreate.realBalanceHint")}
                      </div>
                    </Field>
                  )}
                </div>
              ) : null}
              {autoCreateAccount && accountMode === "live" ? (
                <div className="mt-2 rounded border border-warn/30 bg-warn/10 px-2 py-1 text-[11px] text-warn">
                  {t("autoCreate.liveSafetyHint")}
                </div>
              ) : null}
            </div>

            {fields.length ? (
              <div className="mt-3">
                <button
                  type="button"
                  onClick={() => setManualConfigOpen((v) => !v)}
                  className="btn-ghost text-xs"
                >
                  {manualConfigOpen ? t("manualConfigHide") : t("manualConfigShow")}
                </button>
                {manualConfigOpen ? (
                  <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                    {fields.map((field) => (
                  <Field
                    key={field.name}
                    label={`${field.label || field.name}${field.required ? " *" : ""}`}
                  >
                    <input
                      value={values[field.name] || ""}
                      onChange={(e) =>
                        setValues((prev) => ({ ...prev, [field.name]: e.target.value }))
                      }
                      type={field.sensitive !== false ? "password" : field.kind === "url" ? "url" : "text"}
                      className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                      placeholder={field.placeholder || (field.sensitive === false ? t("publicValue") : t("secretValue"))}
                    />
                    {field.description ? (
                      <div className="mt-1 text-[11px] text-ink-500">{field.description}</div>
                    ) : null}
                  </Field>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}

            {advancedFields.length ? (
              <div className="mt-3">
                <button
                  type="button"
                  onClick={() => setAdvancedConfigOpen((v) => !v)}
                  className="btn-ghost text-xs"
                >
                  {advancedConfigOpen ? t("advancedConfigHide") : t("advancedConfigShow")}
                </button>
                {advancedConfigOpen ? (
                  <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                    {advancedFields.map((field) => (
                    <Field
                      key={field.name}
                      label={`${field.label || field.name}${field.required ? " *" : ""}`}
                    >
                      <input
                        value={values[field.name] || ""}
                        onChange={(e) =>
                          setValues((prev) => ({ ...prev, [field.name]: e.target.value }))
                        }
                        type={field.sensitive !== false ? "password" : field.kind === "url" ? "url" : "text"}
                        className="w-full rounded border border-brand-500/20 bg-ink-900 px-2 py-1 text-ink-100 font-mono"
                        placeholder={field.placeholder || (field.sensitive === false ? t("publicValue") : t("secretValue"))}
                      />
                      {field.description ? (
                        <div className="mt-1 text-[11px] text-ink-500">{field.description}</div>
                      ) : null}
                    </Field>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}

            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <label className="flex items-center gap-2 text-xs text-ink-300">
                <input
                  type="checkbox"
                  checked={activate}
                  onChange={(e) => setActivate(e.target.checked)}
                />
                {t("activateDefault")}
              </label>
              <button
                onClick={() => void saveBinding()}
                disabled={busy === `save:${selected.id}`}
                className="btn-ghost text-xs text-accent-300"
              >
                {busy === `save:${selected.id}` ? t("saving") : t("saveBinding")}
              </button>
              <button
                onClick={() => void testAccountBalance()}
                disabled={
                  !autoCreateAccount ||
                  busy === `balance:${selected.id}` ||
                  (!lastAccountId && !accountIdHint.trim() && !walletId.trim())
                }
                className="btn-ghost text-xs"
              >
                {busy === `balance:${selected.id}` ? t("testingBalance") : t("testBalance")}
              </button>
            </div>
            {balanceTestNotice ? (
              <div className="mt-2 text-[11px] text-ink-300 font-mono">
                {balanceTestNotice}
              </div>
            ) : null}
          </div>
          </div>
        ) : null}
      </div>
    </>
  );
  if (bare) return body;
  return (
    <Card title={t("title")} description={t("description")}>
      {body}
    </Card>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="block text-xs">
      <div className="mb-1 text-ink-400">{label}</div>
      {children}
    </label>
  );
}
