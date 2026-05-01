"use client";

import { useEffect, useState } from "react";
import { Card, Empty, Pill } from "../Page";
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
  const [endpointsRaw, setEndpointsRaw] = useState(
    JSON.stringify(
      {
        markets: { method: "GET", path: "/api/markets" },
      },
      null,
      2,
    ),
  );
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
        let endpoints: Record<string, Record<string, unknown>> | undefined;
        try {
          endpoints = JSON.parse(endpointsRaw || "{}");
        } catch {
          throw new Error("endpoints JSON is invalid");
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
      title="Add exchange (exchange_author)"
      description="4-step guided flow: pick venue → fill metadata → scaffold provider spec → approve. The skill writes a draft to workspace/providers/<venue_id>/ and only activates after approval."
    >
      <div className="flex items-center gap-2 mb-3 text-xs">
        <StepDot active={step >= 1} done={step > 1} label="Venue" />
        <ArrowDot />
        <StepDot active={step >= 2} done={step > 2} label="Scaffold" />
        <ArrowDot />
        <StepDot active={step >= 3} done={step > 3} label="Preview" />
        <ArrowDot />
        <StepDot active={step >= 4} done={step >= 4} label="Approved" />
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
                {m === "ccxt" ? "CCXT-supported" : "Custom HTTP"}
              </button>
            ))}
          </div>
          <div className="text-ink-400">
            {mode === "ccxt"
              ? `Select an exchange already supported by the ccxt library (${ccxtSupported.length} known).`
              : "Author a brand-new venue by describing its REST endpoints — Nerya will scaffold a connector."}
          </div>
          <div>
            <div className="text-ink-500 mb-1">Already configured</div>
            {providers.length === 0 ? (
              <Empty label="No registered providers." />
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
              Continue →
            </button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="space-y-3 text-xs">
          <div className="grid grid-cols-2 gap-2">
            <Field label="venue_id (slug)">
              <input
                value={venueId}
                onChange={(e) => setVenueId(e.target.value.toLowerCase())}
                placeholder={mode === "ccxt" ? "kraken" : "my_dex"}
                className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 font-mono text-ink-100"
              />
            </Field>
            <Field label="Label">
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="Kraken Spot"
                className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100"
              />
            </Field>
          </div>

          {mode === "ccxt" ? (
            <Field label="ccxt id">
              <select
                value={ccxtId}
                onChange={(e) => setCcxtId(e.target.value)}
                className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-200"
              >
                <option value="">— select —</option>
                {ccxtSupported.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Kind">
                  <select
                    value={kind}
                    onChange={(e) =>
                      setKind(e.target.value as typeof kind)
                    }
                    className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-200"
                  >
                    <option value="cex">cex</option>
                    <option value="dex">dex</option>
                    <option value="prediction_market">prediction_market</option>
                    <option value="chain">chain</option>
                  </select>
                </Field>
                <Field label="Install hint">
                  <input
                    value={installHint}
                    onChange={(e) => setInstallHint(e.target.value)}
                    placeholder="pip install foo"
                    className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                  />
                </Field>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Base URL">
                  <input
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="https://api.example.com"
                    className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                  />
                </Field>
                <Field label="Docs URL">
                  <input
                    value={docsUrl}
                    onChange={(e) => setDocsUrl(e.target.value)}
                    placeholder="https://docs.example.com"
                    className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono"
                  />
                </Field>
              </div>
              <Field label="Endpoints (JSON)">
                <textarea
                  value={endpointsRaw}
                  onChange={(e) => setEndpointsRaw(e.target.value)}
                  rows={6}
                  className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-100 font-mono text-[11px]"
                />
              </Field>
            </>
          )}

          <Field label="Notes">
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              placeholder="Why this venue, who approved, etc."
              className="w-full bg-ink-900 border border-brand-500/20 rounded px-2 py-1 text-ink-200"
            />
          </Field>

          <div className="flex justify-between">
            <button
              onClick={() => setStep(1)}
              className="btn-ghost text-xs"
            >
              ← Back
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
              {busy ? "Scaffolding…" : "Scaffold →"}
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
              <Pill tone="warn">{scaffold.state || "pending"}</Pill>
            </div>
          </div>
          <div className="text-ink-400">
            The skill staged a provider spec. Review it on disk if you want, then
            approve to activate. After approval, an operator (you) can store API
            credentials via Settings → Integrations and bind them to a new
            account in the Accounts page.
          </div>
          <div className="flex justify-between">
            <button
              onClick={() => setStep(2)}
              className="btn-ghost text-xs"
            >
              ← Back
            </button>
            <button
              onClick={approve}
              disabled={busy}
              className="btn-ghost text-xs text-accent-300"
            >
              {busy ? "Approving…" : "Approve & activate ✓"}
            </button>
          </div>
        </div>
      ) : null}

      {step === 4 ? (
        <div className="space-y-3 text-xs">
          <div className="rounded-md border border-accent-500/30 bg-accent-500/10 p-3 text-accent-300">
            <div className="font-semibold">Activated.</div>
            <div className="mt-1 text-ink-200">
              <span className="font-mono">{scaffold?.venue_id}</span> is now
              available as a venue. Next:
              <ol className="list-decimal list-inside mt-1 space-y-0.5">
                <li>Store API key/secret via Settings → Integrations → put.</li>
                <li>Add an account on this page referencing the new venue.</li>
                <li>
                  Set the account to <span className="font-mono">paper</span>{" "}
                  first, validate, then promote through shadow → canary → live.
                </li>
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
              Add another
            </button>
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="mt-3 text-[#ef4560] text-xs font-mono">{error}</div>
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
