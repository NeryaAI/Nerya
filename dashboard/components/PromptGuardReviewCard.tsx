"use client";

/**
 * Prompt Guard review card.
 *
 * Surfaces the prompt-guard review queue
 * (``/security/prompt_guard/items``) inside the Action Inbox so suspicious
 * prompts become visible operator work instead of disappearing silently.
 *
 * The card is hidden when the queue is empty.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, ErrorBanner, Pill } from "./Page";
import { clientApi } from "../lib/clientApi";
import type { PromptGuardItem } from "../lib/operatorTypes";

const VERDICT_TONE: Record<string, "ok" | "warn" | "danger" | "brand"> = {
  review: "warn",
  block: "danger",
};

export function PromptGuardReviewCard() {
  const t = useTranslations("promptGuardReview");
  const tCommon = useTranslations("common");
  const [items, setItems] = useState<PromptGuardItem[]>([]);
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const env = await clientApi.promptGuardList("pending");
      if (env.ok) {
        setItems(env.items ?? []);
        setStats((env.stats as Record<string, unknown>) ?? null);
        setError(null);
      } else {
        // Feature disabled by flag - hide the card silently.
        setItems([]);
        setStats(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  async function resolve(item: PromptGuardItem, decision: string) {
    setBusyId(item.id);
    try {
      const env = await clientApi.promptGuardResolve({
        id: item.id,
        decision,
        operator_id: "operator",
      });
      if (!env.ok) {
        setError(env.error || t("resolveFailed"));
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  }

  if (loading) {
    return null;
  }
  if (!items.length && !error) {
    return null;
  }

  return (
    <Card
      title={t("title")}
      description={
        items.length
          ? t("descriptionPending", { count: items.length })
          : t("descriptionEmpty")
      }
      actions={
        items.length ? <Pill tone="warn">{items.length}</Pill> : null
      }
    >
      {error ? <ErrorBanner error={error} /> : null}
      {items.length === 0 ? null : (
        <ul className="space-y-2">
          {items.slice(0, 10).map((item) => (
            <li
              key={item.id}
              className="px-3 py-2 rounded-lg border border-brand-500/15 bg-white/[0.02]"
            >
              <div className="flex items-center gap-2 mb-1">
                <Pill tone={VERDICT_TONE[item.verdict] ?? "warn"}>
                  {item.verdict}
                </Pill>
                <span className="text-[11px] font-mono text-ink-400 truncate">
                  {item.policy}
                </span>
                {item.source_route ? (
                  <span className="text-[11px] text-ink-500 truncate">
                    {item.source_route}
                  </span>
                ) : null}
                <span className="text-[10px] text-ink-500 ml-auto font-mono">
                  {item.ts}
                </span>
              </div>
              {item.excerpt ? (
                <div className="text-[12px] text-ink-200 leading-snug mb-1 break-words">
                  &ldquo;{item.excerpt}&rdquo;
                </div>
              ) : null}
              {item.matched?.length ? (
                <div className="text-[10.5px] text-ink-500 mb-2 font-mono truncate">
                  {t("matched")}: {item.matched.slice(0, 3).join(" · ")}
                  {item.matched.length > 3 ? "  …" : ""}
                </div>
              ) : null}
              <div className="flex flex-wrap items-center gap-2">
                <button
                  disabled={busyId === item.id}
                  onClick={() => resolve(item, "approve_once")}
                  className="text-[11px] px-2 py-0.5 rounded-md border border-brand-500/40 text-brand-200 hover:bg-brand-500/10 disabled:opacity-50"
                >
                  {t("approveOnce")}
                </button>
                <button
                  disabled={busyId === item.id}
                  onClick={() => resolve(item, "reject")}
                  className="text-[11px] px-2 py-0.5 rounded-md border border-rose-500/40 text-rose-200 hover:bg-rose-500/10 disabled:opacity-50"
                >
                  {t("reject")}
                </button>
                <button
                  disabled={busyId === item.id}
                  onClick={() => resolve(item, "trust_source")}
                  className="text-[11px] px-2 py-0.5 rounded-md border border-emerald-500/40 text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-50"
                >
                  {t("trustSource")}
                </button>
                <button
                  disabled={busyId === item.id}
                  onClick={() => resolve(item, "escalate")}
                  className="text-[11px] px-2 py-0.5 rounded-md border border-amber-400/40 text-amber-200 hover:bg-amber-400/10 disabled:opacity-50"
                >
                  {t("escalate")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {stats ? (
        <div className="mt-2 text-[10.5px] text-ink-500 font-mono">
          {tCommon("stats")}: {JSON.stringify(stats)}
        </div>
      ) : null}
    </Card>
  );
}
