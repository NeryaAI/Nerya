"use client";

import { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Sparkline } from "./Sparkline";

export function PageHeader({ title, description, actions, eyebrow }: {
  title: string;
  description?: string;
  actions?: ReactNode;
  eyebrow?: string;
}) {
  return (
    <header className="flex items-start justify-between gap-4 pb-6 mb-8 border-b border-brand-500/15">
      <div className="min-w-0">
        {eyebrow ? (
          <div className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-[0.22em] text-brand-300 mb-2">
            <span className="w-1 h-1 rounded-full bg-fluid-400 shadow-[0_0_8px_rgba(34,211,238,0.7)]" />
            {eyebrow}
          </div>
        ) : null}
        <h2 className="text-[28px] leading-[1.15] font-semibold tracking-tight text-gradient-brand">
          {title}
        </h2>
        {description ? (
          <p className="text-ink-400 text-[13px] mt-2 max-w-2xl leading-relaxed">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex items-center gap-2 pt-1 shrink-0">{actions}</div>
      ) : null}
    </header>
  );
}

export function PageBody({ children }: { children: ReactNode }) {
  return <div className="space-y-8">{children}</div>;
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
    <section className={[
      "card card-hover min-w-0",
      featured
        ? "border-brand-500/30 shadow-[0_0_0_1px_rgba(139,92,246,0.15),0_4px_32px_-8px_rgba(139,92,246,0.25)] relative before:absolute before:inset-x-0 before:top-0 before:h-[2px] before:rounded-t-xl before:bg-gradient-to-r before:from-brand-500/60 before:via-brand-400/80 before:to-fluid-400/40"
        : "",
    ].join(" ")}>
      {(title || actions) && (
        <div className="card-head">
          <div className="min-w-0">
            {title && <h3 className="card-title break-words">{title}</h3>}
            {description && <p className="card-subtle mt-1 break-words">{description}</p>}
          </div>
          {actions ? <div className="shrink-0">{actions}</div> : null}
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
}: {
  label: string;
  value: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "danger" | "brand";
  delta?: ReactNode;
  icon?: ReactNode;
  spark?: number[];
  sparkTone?: "brand" | "accent" | "magenta" | "warn" | "danger";
}) {
  const toneClass = {
    neutral: "text-white",
    ok: "text-accent-400",
    warn: "text-[#f5a524]",
    danger: "text-[#ef4560]",
    brand: "text-brand-200",
  }[tone];
  return (
    <div className="card px-4 py-3.5 relative overflow-hidden card-hover">
      <div className="flex items-start justify-between gap-2">
        <div className="stat-label">{label}</div>
        {icon ? (
          <div className="text-brand-300 opacity-80">{icon}</div>
        ) : null}
      </div>
      <div className={`stat-value mt-1.5 ${toneClass}`}>{value}</div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <div className="text-xs text-ink-400">{delta}</div>
        {spark && spark.length ? (
          <Sparkline
            values={spark}
            width={90}
            height={26}
            tone={sparkTone ?? (tone === "danger" ? "danger" : tone === "warn" ? "warn" : "brand")}
          />
        ) : null}
      </div>
      <div className="pointer-events-none absolute inset-x-0 -bottom-10 h-20 bg-gradient-to-t from-brand-500/5 to-transparent" />
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="embedded-scroll max-h-96 text-xs font-mono text-ink-200 bg-ink-900/80 border border-brand-500/10 rounded-lg p-3">
      {(() => {
        try {
          return JSON.stringify(value, null, 2);
        } catch {
          return String(value);
        }
      })()}
    </pre>
  );
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

export function ErrorBanner({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="rounded-lg border border-[#ef4560]/40 bg-[#ef4560]/10 text-[#ef4560] px-4 py-3 text-sm">
      {msg}
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
}: {
  tone?: "ok" | "warn" | "danger" | "neutral";
  label?: string;
}) {
  const c = {
    ok: "bg-accent-500 shadow-neon",
    warn: "bg-[#f5a524]",
    danger: "bg-[#ef4560]",
    neutral: "bg-ink-500",
  }[tone];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-200">
      <span className={`w-2 h-2 rounded-full ${c}`} />
      {label}
    </span>
  );
}
