"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, Empty, Pill } from "../Page";
import { Select } from "../Select";
import { EndpointMapEditor } from "../EndpointMapEditor";
import { clientApi } from "../../lib/clientApi";

type Step = 1 | 2 | 3 | 4;
type Mode = "ccxt" | "http";

interface ProviderCatalogEntry {
  id: string;
  name?: string;
  label?: string;
  kind?: string;
  source?: string;
  state?: string;
  approved?: boolean;
}

interface ScaffoldResult {
  proposal_id?: string;
  venue_id?: string;
  path?: string;
  state?: string;
}

export function ExchangeAuthorWizard({ onApproved }: { onApproved?: (venueId: string) => void }) {
  const t = useTranslations("exchangeAuthor");
  const [step, setStep] = useState<Step>(1);
  const [mode, setMode] = useState<Mode>("ccxt");
  const [venueId, setVenueId] = useState("");
  const [label, setLabel] = useState("");
  const [notes, setNotes] = useState("");
  const [ccxtId, setCcxtId] = useState("");
  const [kind, setKind] = useState<"cex" | "dex" | "prediction_market" | "chain">("cex");
  const [baseUrl, setBaseUrl] = useState("");
  const [docsUrl, setDocsUrl] = useState("");
  const [installHint, setInstallHint] = useState("");
  const [endpoints, setEndpoints] = useState<
    Record<string, Record<string, unknown>>
  >({
    markets: { method: "GET", path: "/api/markets" },
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderCatalogEntry[]>([]);
  const [scaffold, setScaffold] = useState<ScaffoldResult | null>(null);
  const [ccxtSupported, setCcxtSupported] = useState<string[]>([]);

  useEffect(() => {
    let mounted = true;
    Promise.all([
      clientApi.exchangeProviders().catch(() => ({ providers: [], ccxt_supported: [] })),
    ]).then(([res]) => {
      if (!mounted) return;
      setProviders(((res as Record<string, unknown>).providers as ProviderCatalogEntry[]) || []);
      setCcxtSupported(((res as Record<string, unknown>).ccxt_supported as string[]) || []);
    });
    return () => {
      mounted = false;
    };
  }, []);

  async function runScaffold() {
    setBusy(true);
    setError(null);
    setScaffold(null);
    try {
      if (mode === "ccxt") {
        const res = await clientApi.exchangeAuthorScaffoldCcxt({
          venue_id: venueId.trim(),
          ccxt_id: ccxtId.trim(),
          label: label || undefined,
          notes: notes || undefined,
        });
        setScaffold(res as ScaffoldResult);
      } else {
        if (Object.keys(endpoints).length === 0) {
          throw new Error(t("endpointsInvalid"));
        }
        const res = await clientApi.exchangeAuthorScaffoldHttp({
          venue_id: venueId.trim(),
          kind,
          base_url: baseUrl || undefined,
          docs_url: docsUrl || undefined,
          label: label || undefined,
          install_hint: installHint || undefined,
          endpoints,
          notes: notes || undefined,
        });
        setScaffold(res as ScaffoldResult);
      }
      setStep(3);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!scaffold?.venue_id) return;
    setBusy(true);
    setError(null);
    try {
      await clientApi.exchangeAuthorApprove(scaffold.venue_id);
      setStep(4);
      onApproved?.(scaffold.venue_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={t("title")}
      description={t("description")}
    >
      <div className="flex items-center gap-2 mb-3 text-xs">
        <StepDot active={step >= 1} done={step > 1} label={t("stepVenue")} />
        <ArrowDot />
        <StepDot active={step >= 2} done={step > 2} label={t("stepScaffold")} />
        <ArrowDot />
        <StepDot active={step >= 3} done={step > 3} label={t("stepPreview")} />
        <ArrowDot />
        <StepDot active={step >= 4} done={step >= 4} label={t("stepApproved")} />
      </div>

      {step === 1 ? (
        <div className="space-y-3 text-xs">
          <div className="flex gap-2">
            {(["ccxt", "http"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-3 py-1.5 rounded-md border ${
                  mode === m
                    ? "border-brand-400 bg-brand-500/20 text-brand-100"
                    : "border-brand-500/20 text-ink-300"
                }`}
              >
                {m === "ccxt" ? t("ccxtSupported") : t("customHttp")}
              </button>
            ))}
          </div>
          <div className="text-ink-400">
            {mode === "ccxt"
              ? t("ccxtDesc", { count: ccxtSupported.length })
              : t("httpDesc")}
          </div>
          <div>
            <div className="text-ink-500 mb-1">{t("alreadyConfigured")}</div>
            {providers.length === 0 ? (
              <Empty label={t("noRegistered")} />
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {providers.map((p) => (
                  <Pill
                    key={p.id}
                    tone={p.approved ? "ok" : p.state === "approved" ? "ok" : "neutral"}
                  >
                    {p.id}
                  </Pill>
                ))}
              </div>
            )}
          </div>
          <div className="flex justify-end">
            <button
              onClick={() => setStep(2)}
              className="btn-ghost text-xs text-accent-300"
            >
              {t("continue")}
            </button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="space-y-3 text-xs">
          <div className="grid grid-cols-2 gap-2">
            <Field label={t("venueIdLabel")}>
              <input
                value={venueId}
                onChange={(e) => setVenueId(e.target.value.toLowerCase())}
                placeholder={mode === "ccxt" ? "kraken" : "my_dex"}
                className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 font-mono text-ink-100"
              />
            </Field>
            <Field label={t("labelField")}>
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="Kraken Spot"
                className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100"
              />
            </Field>
          </div>

          {mode === "ccxt" ? (
            <Field label={t("ccxtIdLabel")}>
              <Select
                value={ccxtId}
                onChange={(value) => setCcxtId(value)}
                options={[
                  { value: "", label: t("selectPlaceholder") },
                  ...ccxtSupported.map((id) => ({ value: id, label: id })),
                ]}
                size="sm"
                ariaLabel={t("ccxtIdLabel")}
              />
            </Field>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-2">
                <Field label={t("kindLabel")}>
                  <Select<typeof kind>
                    value={kind}
                    onChange={(value) => setKind(value)}
                    options={[
                      { value: "cex", label: "cex" },
                      { value: "dex", label: "dex" },
                      { value: "prediction_market", label: "prediction_market" },
                      { value: "chain", label: "chain" },
                    ]}
                    size="sm"
                    ariaLabel={t("kindLabel")}
                  />
                </Field>
                <Field label={t("installHintLabel")}>
                  <input
                    value={installHint}
                    onChange={(e) => setInstallHint(e.target.value)}
                    placeholder="pip install foo"
                    className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                  />
                </Field>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label={t("baseUrlLabel")}>
                  <input
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="https://api.example.com"
                    className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                  />
                </Field>
                <Field label={t("docsUrlLabel")}>
                  <input
                    value={docsUrl}
                    onChange={(e) => setDocsUrl(e.target.value)}
                    placeholder="https://docs.example.com"
                    className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                  />
                </Field>
              </div>
              <Field label={t("endpointsLabel")}>
                <EndpointMapEditor
                  value={endpoints}
                  onChange={setEndpoints}
                />
              </Field>
            </>
          )}

          <Field label={t("notesLabel")}>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              placeholder={t("notesPlaceholder")}
              className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-200"
            />
          </Field>

          <div className="flex justify-between">
            <button
              onClick={() => setStep(1)}
              className="btn-ghost text-xs"
            >
              {t("back")}
            </button>
            <button
              onClick={runScaffold}
              disabled={
                busy ||
                !venueId ||
                (mode === "ccxt" && !ccxtId) ||
                (mode === "http" && !baseUrl)
              }
              className="btn-ghost text-xs text-accent-300"
            >
              {busy ? t("scaffolding") : t("scaffoldBtn")}
            </button>
          </div>
        </div>
      ) : null}

      {step === 3 && scaffold ? (
        <div className="space-y-3 text-xs">
          <div className="rounded-md border border-brand-500/20 p-3 bg-ink-900/40 space-y-1.5 font-mono">
            <div>
              <span className="text-ink-500">venue_id:</span>{" "}
              <span className="text-brand-200">{scaffold.venue_id}</span>
            </div>
            <div>
              <span className="text-ink-500">proposal_id:</span>{" "}
              <span className="text-ink-200">{scaffold.proposal_id}</span>
            </div>
            <div>
              <span className="text-ink-500">path:</span>{" "}
              <span className="text-ink-200">{scaffold.path}</span>
            </div>
            <div>
              <span className="text-ink-500">state:</span>{" "}
              <Pill tone="warn">{scaffold.state || t("pending")}</Pill>
            </div>
          </div>
          <div className="text-ink-400">
            {t("stagedSpecDesc")}
          </div>
          <div className="flex justify-between">
            <button
              onClick={() => setStep(2)}
              className="btn-ghost text-xs"
            >
              {t("back")}
            </button>
            <button
              onClick={approve}
              disabled={busy}
              className="btn-ghost text-xs text-accent-300"
            >
              {busy ? t("approving") : t("approveActivate")}
            </button>
          </div>
        </div>
      ) : null}

      {step === 4 ? (
        <div className="space-y-3 text-xs">
          <div className="rounded-md border border-accent-500/30 bg-accent-500/10 p-3 text-accent-300">
            <div className="font-semibold">{t("activated")}</div>
            <div className="mt-1 text-ink-200">
              <span className="font-mono">{scaffold?.venue_id}</span>{" "}
              {t("nowAvailableNext")}
              <ol className="list-decimal list-inside mt-1 space-y-0.5">
                <li>{t("stepStoreKey")}</li>
                <li>{t("stepAddAccount")}</li>
                <li>{t("stepPromote")}</li>
              </ol>
            </div>
          </div>
          <div className="flex justify-end">
            <button
              onClick={() => {
                setStep(1);
                setScaffold(null);
              }}
              className="btn-ghost text-xs"
            >
              {t("addAnother")}
            </button>
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="mt-3 text-danger text-xs font-mono">{error}</div>
      ) : null}
    </Card>
  );
}

function StepDot({
  active,
  done,
  label,
}: {
  active: boolean;
  done: boolean;
  label: string;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span
        className={`w-4 h-4 rounded-full flex items-center justify-center text-[9px] ${
          done
            ? "bg-accent-500 text-ink-900"
            : active
              ? "bg-brand-500 text-white"
              : "bg-ink-800 text-ink-400"
        }`}
      >
        {done ? "✓" : ""}
      </span>
      <span className={done || active ? "text-ink-200" : "text-ink-500"}>
        {label}
      </span>
    </span>
  );
}

function ArrowDot() {
  return <span className="text-ink-600">›</span>;
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
      <div className="text-ink-400 mb-1">{label}</div>
      {children}
    </div>
  );
}
