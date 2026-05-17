"use client";

/**
 * Setup wizard mounted at /setup.
 *
 * Walks a fresh-install operator through seven domains — password,
 * LLM model, gateway, memory, browser, account, search — by reusing
 * the existing `SettingsWorkspace` cards via its `forceSection` prop.
 * This component owns only the wizard chrome: the stepper, the
 * per-step header, and the Skip/Back/Next/Finish footer. No
 * SettingsWorkspace logic is duplicated.
 *
 * Step state survives page reloads via `localStorage["nerya.setup.step"]`.
 *
 * Why a separate wizard component (not a flag on SettingsWorkspace)?
 *
 * The Settings page is a power-user surface — every card is visible
 * at once and the user is expected to know what they're looking for.
 * The wizard is a sequential, one-screen-at-a-time guide that's
 * appropriate for first-run / re-onboarding flows. They share the
 * underlying state hooks via `forceSection`, but the chrome is
 * different on purpose.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, PageBody, PageHeader, Pill } from "./Page";
import { SettingsWorkspace, type ForceSectionKey } from "./SettingsWorkspace";
import { clientApi } from "../lib/clientApi";
import type { SetupReadinessEnvelope, ReadinessCheck } from "../lib/operatorTypes";

type WizardStepKey =
  | "password"
  | "llm"
  | "gateway"
  | "memory"
  | "browser"
  | "account"
  | "search";

const STEP_ORDER: readonly WizardStepKey[] = [
  "password",
  "llm",
  "gateway",
  "memory",
  "browser",
  "account",
  "search",
] as const;

const STEP_STORAGE_KEY = "nerya.setup.step";

// Map each wizard step to either:
//   - a `ForceSectionKey` so we mount the matching SettingsWorkspace
//     panel inline (the cleanest reuse path), or
//   - `null` when the step renders custom content (currently: account).
// IMPORTANT: every wizard step maps to either a SettingsWorkspace
// section (so we reuse those cards) or `null` for a custom step.
// ``gateway`` USED to map to ``runtime``, which surfaced runtime
// feature flags + Network proxy + Tunnels — none of which are
// "Gateway" in the operator's mental model. It now mounts the real
// per-platform messaging-channel configuration so the wizard's
// Gateway step actually configures Telegram / Discord / Slack /
// Feishu / WhatsApp / WeCom / Mattermost / Matrix / Email / SMS /
// Home Assistant / Webhook / etc.
const STEP_SECTIONS: Record<WizardStepKey, ForceSectionKey | null> = {
  password: "access",
  llm: "models",
  gateway: "gateway",
  memory: "memory",
  browser: "browsers",
  account: null,
  search: "search",
};

// Setup-readiness check `name` → wizard step. Used to colour the
// stepper pills from the live readiness envelope so users see at a
// glance which step still needs work.
const READINESS_TO_STEP: Record<string, WizardStepKey> = {
  "LLM provider": "llm",
  "Trading account": "account",
  // The remaining readiness checks (Strategy / Risk policy / Wallet)
  // don't have a 1:1 wizard step. They surface in the readiness panel
  // at the bottom of the page so the operator still sees them.
};


// ---------------------------------------------------------------------------
// Stepper
// ---------------------------------------------------------------------------

type StepStatus = "pending" | "ok" | "warn" | "blocked";

function Stepper({
  current,
  statuses,
  labels,
  onJump,
}: {
  current: WizardStepKey;
  statuses: Record<WizardStepKey, StepStatus>;
  labels: Record<WizardStepKey, string>;
  onJump: (step: WizardStepKey) => void;
}) {
  return (
    <nav aria-label="Setup wizard steps" className="mb-5">
      <ol className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-7">
        {STEP_ORDER.map((step, index) => {
          const status = statuses[step];
          const selected = step === current;
          const tone =
            status === "blocked"
              ? "border-rose-500/50 text-rose-200"
              : status === "warn"
                ? "border-amber-500/50 text-amber-200"
                : status === "ok"
                  ? "border-emerald-500/50 text-emerald-200"
                  : "border-[color:var(--line)] text-ink-300";
          return (
            <li key={step}>
              <button
                type="button"
                aria-current={selected ? "step" : undefined}
                onClick={() => onJump(step)}
                className={[
                  "block w-full rounded-lg border px-3 py-3 text-left transition-colors",
                  selected
                    ? "border-brand-400/60 bg-brand-500/10 text-white"
                    : `${tone} hover:border-brand-500/30 hover:text-ink-100`,
                ].join(" ")}
              >
                <span className="block text-[11px] font-mono text-ink-500">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span className="mt-0.5 block text-[14px] font-medium">
                  {labels[step]}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}


// ---------------------------------------------------------------------------
// Account step (custom — not backed by a forceSection)
// ---------------------------------------------------------------------------

function AccountStep() {
  const t = useTranslations("setupWizard");
  const [accountsCount, setAccountsCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    clientApi
      .accountsList()
      .then((res) => {
        if (cancelled) return;
        setAccountsCount((res?.accounts || []).length);
      })
      .catch(() => {
        if (!cancelled) setAccountsCount(0);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card
      title={t("steps.account")}
      description={t("steps.accountDesc")}
      actions={
        accountsCount !== null ? (
          <Pill tone={accountsCount > 0 ? "ok" : "warn"}>
            {accountsCount} account{accountsCount === 1 ? "" : "s"}
          </Pill>
        ) : null
      }
    >
      <div className="space-y-3">
        <div className="rounded-lg border border-[color:var(--line)] bg-ink-950/40 p-3 text-[13px] text-ink-200">
          {accountsCount === 0 ? (
            <span className="text-amber-200">{t("account.noneConfigured")}</span>
          ) : (
            <>
              <div className="text-ink-400">{t("account.current")}</div>
              <div className="mt-1 font-mono text-[12px] text-ink-100">
                {accountsCount} configured
              </div>
            </>
          )}
        </div>
        <p className="text-[12px] leading-5 text-ink-500">
          {t("account.openAccountsHint")}
        </p>
        <div className="flex flex-wrap gap-2">
          <Link
            href="/accounts"
            className="btn btn-primary"
          >
            {t("openAccounts")}
          </Link>
        </div>
      </div>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// Readiness summary (bottom of wizard)
// ---------------------------------------------------------------------------

function ReadinessSummary({ env }: { env: SetupReadinessEnvelope | null }) {
  if (!env) return null;
  const checks = env.data.checks || [];
  if (!checks.length) return null;
  return (
    <Card
      title="Readiness checks"
      description={env.summary}
      actions={<Pill tone={env.status === "ok" ? "ok" : env.status === "warn" ? "warn" : "danger"}>{env.status}</Pill>}
    >
      <ul className="space-y-2">
        {checks.map((chk: ReadinessCheck) => (
          <li
            key={chk.name}
            className="flex items-start gap-3 rounded-lg border border-[color:var(--line)] px-3 py-2"
          >
            <span
              className={[
                "mt-1 inline-block h-2 w-2 rounded-full",
                chk.status === "ok"
                  ? "bg-emerald-400"
                  : chk.status === "warn"
                    ? "bg-amber-400"
                    : "bg-rose-400",
              ].join(" ")}
            />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-ink-100">{chk.name}</div>
              <div className="mt-0.5 text-[12px] text-ink-400">{chk.summary}</div>
            </div>
          </li>
        ))}
      </ul>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function SetupWizard() {
  const t = useTranslations("setupWizard");

  const [stepIndex, setStepIndex] = useState(0);
  const [readiness, setReadiness] = useState<SetupReadinessEnvelope | null>(null);
  const [quickMode, setQuickMode] = useState(false);

  // Hydrate persisted step from localStorage on the client only. Also
  // detect `?mode=quick` to enable the single-step LLM-only view —
  // the CLI's `nerya setup --quick --web` appends this query string so
  // both surfaces share a single onboarding ergonomic.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("mode") === "quick") {
      setQuickMode(true);
      // Force the LLM step on first paint regardless of any persisted
      // localStorage cursor.
      const llmIdx = STEP_ORDER.indexOf("llm");
      if (llmIdx >= 0) setStepIndex(llmIdx);
      return;
    }
    const raw = window.localStorage.getItem(STEP_STORAGE_KEY);
    if (!raw) return;
    const idx = STEP_ORDER.indexOf(raw as WizardStepKey);
    if (idx >= 0) setStepIndex(idx);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STEP_STORAGE_KEY, STEP_ORDER[stepIndex]);
  }, [stepIndex]);

  // Live readiness polling — same 60s cadence as SetupReadinessCard.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const next = await clientApi.setupReadiness();
        if (!cancelled) setReadiness(next);
      } catch {
        // ignore — readiness is informational
      }
    }
    load();
    const id = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const labels = useMemo<Record<WizardStepKey, string>>(
    () => ({
      password: t("steps.password"),
      llm: t("steps.llm"),
      gateway: t("steps.gateway"),
      memory: t("steps.memory"),
      browser: t("steps.browser"),
      account: t("steps.account"),
      search: t("steps.search"),
    }),
    [t]
  );

  const descriptions = useMemo<Record<WizardStepKey, string>>(
    () => ({
      password: t("steps.passwordDesc"),
      llm: t("steps.llmDesc"),
      gateway: t("steps.gatewayDesc"),
      memory: t("steps.memoryDesc"),
      browser: t("steps.browserDesc"),
      account: t("steps.accountDesc"),
      search: t("steps.searchDesc"),
    }),
    [t]
  );

  // Derive per-step status from the live readiness envelope.
  const stepStatuses = useMemo<Record<WizardStepKey, StepStatus>>(() => {
    const base: Record<WizardStepKey, StepStatus> = {
      password: "pending",
      llm: "pending",
      gateway: "pending",
      memory: "pending",
      browser: "pending",
      account: "pending",
      search: "pending",
    };
    if (!readiness) return base;
    for (const chk of readiness.data.checks || []) {
      const key = READINESS_TO_STEP[chk.name];
      if (key) {
        base[key] =
          chk.status === "ok"
            ? "ok"
            : chk.status === "warn"
              ? "warn"
              : "blocked";
      }
    }
    return base;
  }, [readiness]);

  const currentStep = STEP_ORDER[stepIndex];
  const sectionKey = STEP_SECTIONS[currentStep];
  const isLast = stepIndex === STEP_ORDER.length - 1;
  const blocking = readiness?.data.blocking?.length || 0;

  function jumpTo(step: WizardStepKey) {
    const idx = STEP_ORDER.indexOf(step);
    if (idx >= 0) setStepIndex(idx);
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
        actions={
          <Link href="/settings" className="btn btn-ghost">
            {t("openSettings")}
          </Link>
        }
      />

      {quickMode ? null : (
        <Stepper
          current={currentStep}
          statuses={stepStatuses}
          labels={labels}
          onJump={jumpTo}
        />
      )}

      <Card
        title={labels[currentStep]}
        description={
          quickMode ? t("quickModeDescription") : descriptions[currentStep]
        }
        actions={
          quickMode ? (
            <Pill tone="brand">{t("quickModeBadge")}</Pill>
          ) : (
            <span className="font-mono text-[11px] text-ink-500">
              {t("stepLabel", {
                current: stepIndex + 1,
                total: STEP_ORDER.length,
              })}
            </span>
          )
        }
      >
        <div className="mt-2">
          {sectionKey ? (
            // Reuse the full Settings panel for this domain. The wizard
            // does NOT re-implement password / tier / gateway editing —
            // it just mounts the existing cards inline. In quick mode
            // we pass `compactLlm` so the tier-assignment matrix stays
            // hidden — `nerya setup` (full wizard) is the surface for
            // that, not the one-question quick path.
            <SettingsWorkspace
              forceSection={sectionKey}
              compactLlm={quickMode && sectionKey === "models"}
            />
          ) : (
            <AccountStep />
          )}
        </div>
      </Card>

      {quickMode ? (
        <Card
          title={t("quickFinishTitle")}
          description={t("quickFinishDescription")}
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-[12px] leading-5 text-ink-400">
              {t("quickFinishHint")}{" "}
              <span className="font-mono text-ink-200">nerya setup</span>
            </p>
            <Link href="/" className="btn btn-primary">
              {t("finish")} →
            </Link>
          </div>
        </Card>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2 pt-2">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => setStepIndex((idx) => Math.max(0, idx - 1))}
            disabled={stepIndex === 0}
          >
            ← {t("back")}
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setStepIndex((idx) => Math.min(STEP_ORDER.length - 1, idx + 1))}
              disabled={isLast}
            >
              {t("skip")}
            </button>
            {!isLast ? (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => setStepIndex((idx) => Math.min(STEP_ORDER.length - 1, idx + 1))}
              >
                {t("next")} →
              </button>
            ) : (
              <Link href="/" className="btn btn-primary">
                {t("finish")} →
              </Link>
            )}
          </div>
        </div>
      )}

      {isLast && !quickMode ? (
        <Card
          title={t("finishedTitle")}
          description={t("finishedDescription")}
        >
          <div className="flex flex-wrap gap-2">
            <Link href="/" className="btn btn-primary">
              {t("runtimeButton")}
            </Link>
            <Link href="/settings" className="btn btn-ghost">
              {t("settingsButton")}
            </Link>
          </div>
          {blocking ? (
            <p className="mt-3 text-[12px] text-amber-200">
              {t("blockingRemaining", {
                count: blocking,
                command: "nerya setup",
              })}
            </p>
          ) : (
            <p className="mt-3 text-[12px] text-emerald-300">
              {t("readyToStart")}
            </p>
          )}
        </Card>
      ) : null}

      <ReadinessSummary env={readiness} />

      <div className="text-center text-[11px] font-mono text-ink-500">
        {t("rerunHint")} <span className="text-ink-300">nerya setup</span>
      </div>
    </PageBody>
  );
}
