"use client";

import { ReactNode, useEffect, useId, useState } from "react";
import { useTranslations } from "next-intl";
import { Sparkline } from "./Sparkline";
import { JsonView } from "./JsonView";
import { ChevronRightIcon } from "./icons";

export function PageHeader({ title, description, actions, eyebrow }: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: string;
}) {
  return (
    <header className="mb-6 flex flex-col gap-4 border-b border-[color:var(--line)] pb-4 sm:mb-7 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        {eyebrow ? (
          <div className="text-[12px] font-medium text-brand-300 mb-1.5">
            {eyebrow}
          </div>
        ) : null}
        <h1 className="text-xl leading-tight font-semibold tracking-tight text-[color:var(--text-base)] sm:text-2xl">
          {title}
        </h1>
        {description ? (
          <div className="text-[13px] text-[color:var(--text-muted)] mt-1.5 max-w-2xl leading-relaxed">
            {description}
          </div>
        ) : null}
      </div>
      {actions ? (
        <div className="flex w-full flex-wrap items-center gap-2 pt-1 sm:w-auto sm:justify-end sm:shrink-0">
          {actions}
        </div>
      ) : null}
    </header>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
  divider = true,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  divider?: boolean;
}) {
  const hasHead = Boolean(title || actions || description);
  return (
    <section className="min-w-0">
      {hasHead ? (
        <div
          className={
            divider
              ? "mb-4 flex flex-wrap items-start justify-between gap-3 border-b border-[color:var(--line)] pb-3"
              : "mb-3 flex flex-wrap items-start justify-between gap-3"
          }
        >
          <div className="min-w-0">
            {title ? (
              <h3 className="text-[15px] font-medium text-[color:var(--text-base)]">
                {title}
              </h3>
            ) : null}
            {description ? (
              <p className="mt-0.5 text-[13px] text-[color:var(--text-muted)]">
                {description}
              </p>
            ) : null}
          </div>
          {actions ? (
            <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
          ) : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function PageBody({ children }: { children: ReactNode }) {
  return <div className="space-y-6">{children}</div>;
}

export function LoadingState({ rows = 3, label }: { rows?: number; label?: string }) {
  const t = useTranslations("common");
  return <div role="status" aria-busy="true" className="space-y-3 px-4 py-5">
    <span className="sr-only">{label ?? t("loading")}</span>
    {Array.from({ length: rows }, (_, index) => <div key={index} aria-hidden="true" className="flex items-center gap-3">
      <div className="skeleton h-9 w-9 shrink-0 rounded-lg" />
      <div className="min-w-0 flex-1 space-y-2"><div className="skeleton h-3 w-2/3" /><div className="skeleton h-2.5 w-1/3" /></div>
    </div>)}
  </div>;
}

/**
 * `<Advanced>` — Progressive-disclosure container for low-frequency or
 * expert-only modules. Use it instead of a second `<Card>` whenever a
 * subsection is:
 *   - viewed by ≤10% of sessions (e.g. raw JSON, debug envelopes)
 *   - only touched by operators / developers (e.g. tunnels, write rules)
 *   - stable after first config (e.g. API keys, embedding models)
 *
 * The collapsed state is a single-line row; expanded content lives inline
 * (no extra outer frame) so multiple `<Advanced>` can stack tidily inside
 * one Section. `storageKey` opts the user into localStorage memory so an
 * opened panel stays open on the next visit.
 */
export function Advanced({
  title,
  description,
  count,
  defaultOpen = false,
  storageKey,
  open: controlledOpen,
  onToggle,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  count?: number | string;
  defaultOpen?: boolean;
  storageKey?: string;
  open?: boolean;
  onToggle?: (next: boolean) => void;
  children: ReactNode;
}) {
  const controlled = controlledOpen != null;
  const contentId = useId();
  // The server and first client render must agree; restore preferences after mount.
  const [internalOpen, setInternalOpen] = useState(defaultOpen);
  const [restoredKey, setRestoredKey] = useState<string | null>(null);
  useEffect(() => {
    if (controlled || !storageKey) return;
    let next = defaultOpen;
    try {
      const saved = window.localStorage.getItem(storageKey);
      if (saved === "1" || saved === "0") next = saved === "1";
    } catch { /* Storage may be unavailable in private browsing. */ }
    setInternalOpen(next);
    setRestoredKey(storageKey);
  }, [controlled, storageKey, defaultOpen]);
  const open = controlled ? Boolean(controlledOpen) : internalOpen;
  useEffect(() => {
    if (controlled || !storageKey || restoredKey !== storageKey) return;
    try { window.localStorage.setItem(storageKey, internalOpen ? "1" : "0"); } catch { /* Preference only. */ }
  }, [controlled, storageKey, internalOpen, restoredKey]);
  function toggle() {
    const next = !open;
    if (!controlled) setInternalOpen(next);
    if (onToggle) onToggle(next);
  }
  return (
    <section className="mt-4 border-t border-[color:var(--line)] pt-3">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-controls={contentId}
        className="flex min-h-9 w-full items-center justify-between gap-3 py-1 text-left text-[13px] text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]"
      >
        <span className="flex min-w-0 items-center gap-2">
          <ChevronRightIcon
            size={14}
            className={`shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
          />
          <span className="truncate font-medium text-[color:var(--text-base)]">{title}</span>
          {count != null && count !== "" ? (
            <span className="shrink-0 text-[12px] text-[color:var(--text-muted)]">· {count}</span>
          ) : null}
        </span>
        {/* The chevron already communicates open/closed — the old
            trailing "Show/Hide" label was a second control for the same
            action on every collapsible row. */}
      </button>
      {open && description ? (
        <p className="ml-5 mt-1 text-[12px] text-[color:var(--text-muted)]">{description}</p>
      ) : null}
      <div id={contentId} hidden={!open} className={open ? "mt-3" : undefined}>{open ? children : null}</div>
    </section>
  );
}

export function Card({
  title,
  description,
  children,
  actions,
  padded = true,
  featured = false,
}: {
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  padded?: boolean;
  featured?: boolean;
}) {
  return (
    <section
      className={[
        "card card-hover min-w-0 relative",
        padded ? "card-padded" : "card-unpadded",
        featured ? "card-featured" : "",
      ].join(" ")}
    >
      {(title || description || actions) && (
        <div className="card-head">
          <div className="min-w-0">
            {title && (
              <h3 className="card-title break-words">
                {featured ? (
                  <span
                    aria-hidden
                    className="mr-2 inline-block h-1.5 w-1.5 translate-y-[-2px] rounded-full bg-brand-400 align-middle"
                  />
                ) : null}
                {title}
              </h3>
            )}
            {description && <p className="card-subtle mt-1 break-words">{description}</p>}
          </div>
          {actions ? (
            <div className="flex max-w-full flex-wrap items-center gap-2 sm:shrink-0">
              {actions}
            </div>
          ) : null}
        </div>
      )}
      <div className={padded ? "px-4 py-3.5" : ""}>{children}</div>
    </section>
  );
}

export function Kpi({
  label,
  value,
  tone = "neutral",
  delta,
  icon,
  spark,
  sparkTone,
  inline = false,
}: {
  label: string;
  value: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "danger" | "brand";
  delta?: ReactNode;
  icon?: ReactNode;
  spark?: number[];
  sparkTone?: "brand" | "accent" | "magenta" | "warn" | "danger";
  /**
   * Inline mode: no card chrome, no sparkline, no icon, no decorative
   * gradient. Renders as a label + value stack suitable for a row of
   * 3-4 numbers at the top of a page. Use this in place of mini-cards.
   */
  inline?: boolean;
}) {
  // Tone colours use the console-wide status tokens (text-ok/warn/danger),
  // matching Pill/StatusDot so every KPI number sits in the token system.
  const toneClass = {
    neutral: "text-[color:var(--text-base)]",
    ok: "text-ok",
    warn: "text-warn",
    danger: "text-danger",
    brand: "text-brand-300",
  }[tone];

  if (inline) {
    return (
      <div className="min-w-0">
        <div className="stat-label">{label}</div>
        <div className={`stat-value mt-1 ${toneClass}`}>{value}</div>
        {delta ? (
          <div className="mt-0.5 text-[12px] text-[color:var(--text-muted)]">{delta}</div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="card px-4 py-3.5 card-hover">
      <div className="flex items-start justify-between gap-2">
        <div className="stat-label">{label}</div>
        {icon ? <div className="text-brand-300 opacity-80">{icon}</div> : null}
      </div>
      <div className={`stat-value mt-1.5 ${toneClass}`}>{value}</div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <div className="text-[12px] text-[color:var(--text-muted)]">{delta}</div>
        {spark && spark.length ? (
          <Sparkline
            values={spark}
            width={90}
            height={26}
            tone={
              sparkTone ??
              (tone === "danger" ? "danger" : tone === "warn" ? "warn" : "brand")
            }
          />
        ) : null}
      </div>
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return <JsonView value={value} />;
}

export function Empty({
  label,
  title,
  subtitle,
  action,
}: {
  label?: string;
  title?: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  const t = useTranslations("pageCommon");
  const heading = title || label || t("noData");
  return (
    <div className="py-10 text-center text-sm text-[color:var(--text-muted)]">
      <div className="font-medium text-[color:var(--text-base)]">{heading}</div>
      {subtitle ? <div className="mx-auto mt-2 max-w-md text-[13px] leading-relaxed">{subtitle}</div> : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}

/** Loading failures stay visible until resolved; transient action feedback uses toast(). */
export function ErrorBanner({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const t = useTranslations("ui");
  if (error == null || error === false || error === "") return null;
  const message = error instanceof Error ? error.message : String(error);
  if (!message || message === "null" || message === "undefined") return null;
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[color:var(--err)] bg-[color:var(--card)] px-4 py-3 text-sm">
      <span className="min-w-0 flex-1 break-words text-[color:var(--text-base)]">{message}</span>
      {onRetry ? <button type="button" className="btn btn-ghost shrink-0" onClick={onRetry}>{t("retry")}</button> : null}
    </div>
  );
}

export function Pill({ tone = "neutral", children }: {
  tone?: "neutral" | "ok" | "warn" | "danger" | "brand";
  children: ReactNode;
}) {
  const tones = {
    neutral: "pill",
    ok: "pill pill-ok",
    warn: "pill pill-warn",
    danger: "pill pill-err",
    brand: "pill pill-brand",
  } as const;
  return <span className={tones[tone]}>{children}</span>;
}

export function StatusDot({
  tone = "ok",
  label,
  dot = true,
}: {
  tone?: "ok" | "warn" | "danger" | "neutral" | "brand";
  label?: string;
  /**
   * Whether to render the coloured dot. When false, the label is
   * still tinted so meaning is preserved without an extra glyph.
   * 2026-05 redesign: prefer colour + text over decorative dots.
   */
  dot?: boolean;
}) {
  const textClass = {
    ok: "text-ok",
    warn: "text-warn",
    danger: "text-danger",
    brand: "text-brand-300",
    neutral: "text-[color:var(--text-muted)]",
  }[tone];
  const dotClass = {
    ok: "bg-ok",
    warn: "bg-warn",
    danger: "bg-danger",
    brand: "bg-brand-400",
    neutral: "bg-[color:var(--text-muted)]",
  }[tone];
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] ${textClass}`}>
      {dot ? <span className={`w-1.5 h-1.5 rounded-full ${dotClass}`} /> : null}
      {label}
    </span>
  );
}
