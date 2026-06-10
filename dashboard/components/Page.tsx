"use client";

import { ReactNode, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Sparkline } from "./Sparkline";
import { JsonView } from "./JsonView";
import { ChevronRightIcon } from "./icons";
import { toast as dispatchToast } from "../lib/dialogs";

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
        <h2 className="text-[22px] leading-[1.2] font-medium text-[color:var(--text-base)]">
          {title}
        </h2>
        {description ? (
          <p className="text-[13px] text-[color:var(--text-muted)] mt-1.5 max-w-2xl leading-relaxed">
            {description}
          </p>
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
              ? "mb-4 flex items-start justify-between gap-3 border-b border-[color:var(--line)] pb-3"
              : "mb-3 flex items-start justify-between gap-3"
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
  return <div className="space-y-8">{children}</div>;
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
  const tCommon = useTranslations("pageCommon");
  const controlled = controlledOpen != null;
  const [internalOpen, setInternalOpen] = useState<boolean>(() => {
    if (controlled) return false;
    if (storageKey && typeof window !== "undefined") {
      try {
        const saved = window.localStorage.getItem(storageKey);
        if (saved != null) return saved === "1";
      } catch {
      }
    }
    return defaultOpen;
  });
  const open = controlled ? Boolean(controlledOpen) : internalOpen;
  useEffect(() => {
    if (controlled || !storageKey || typeof window === "undefined") return;
    try {
      window.localStorage.setItem(storageKey, internalOpen ? "1" : "0");
    } catch {
    }
  }, [controlled, storageKey, internalOpen]);
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
        className="flex w-full items-center justify-between gap-3 py-1 text-left text-[13px] text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]"
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
        <span className="shrink-0 text-[12px]">{open ? tCommon("advancedHide") : tCommon("advancedShow")}</span>
      </button>
      {open && description ? (
        <p className="ml-5 mt-1 text-[12px] text-[color:var(--text-muted)]">{description}</p>
      ) : null}
      {open ? <div className="mt-3">{children}</div> : null}
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
        featured ? "card-featured" : "",
      ].join(" ")}
    >
      {(title || actions) && (
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
      <div className={padded ? "px-5 py-4" : ""}>{children}</div>
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
  const toneClass = {
    neutral: "text-[color:var(--text-base)]",
    ok: "text-emerald-500",
    warn: "text-amber-500",
    danger: "text-rose-500",
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
}: {
  label?: string;
  title?: string;
  subtitle?: string;
}) {
  const t = useTranslations("pageCommon");
  const heading = title || label || t("noData");
  return (
    <div className="text-ink-400 text-sm italic py-8 text-center">
      <div>{heading}</div>
      {subtitle ? (
        <div className="mt-1 text-[12px] text-ink-500">{subtitle}</div>
      ) : null}
    </div>
  );
}

/**
 * `<ErrorBanner>` is a **side-effect-only** component since the
 * Toast migration. It used to render a red bar inline at the
 * top of the page; that pattern stacked multiple bars on busy pages and
 * pushed content downward. Now it fires a single bottom-right toast
 * whenever the `error` prop changes and renders nothing — every call
 * site (≈24 pages) keeps its existing `<ErrorBanner error={…} />` JSX
 * unchanged, so this is a zero-touch migration.
 *
 * Empty / falsy errors are ignored. Repeated identical messages within
 * the same render cycle are de-duped (we keep the last surfaced
 * message in a ref) so a polling loop that keeps re-setting the same
 * error doesn't fire a new toast every render.
 */
export function ErrorBanner({ error }: { error: unknown }) {
  const lastRef = useRef<string | null>(null);
  useEffect(() => {
    if (error == null || error === false) return;
    const msg =
      error instanceof Error
        ? error.message
        : typeof error === "string"
        ? error
        : String(error);
    if (!msg || msg === "null" || msg === "undefined") return;
    if (lastRef.current === msg) return;
    lastRef.current = msg;
    dispatchToast({ message: msg, tone: "error", durationMs: 6000 });
  }, [error]);
  return null;
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
    ok: "text-emerald-500",
    warn: "text-amber-500",
    danger: "text-rose-500",
    brand: "text-brand-300",
    neutral: "text-[color:var(--text-muted)]",
  }[tone];
  const dotClass = {
    ok: "bg-emerald-500",
    warn: "bg-amber-500",
    danger: "bg-rose-500",
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
