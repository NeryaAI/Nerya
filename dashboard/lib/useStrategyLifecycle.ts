"use client";

/**
 * Shared strategy lifecycle state machine (rename / pause / delete).
 *
 * The strategies list cards and the strategy detail page used to keep
 * two near-identical copies of these flows (~130 lines) with a
 * three-layer nested confirm on the delete path. This hook collapses
 * all of it into one place:
 *
 * - one confirm per action — delete probes open positions with a
 *   dry-run close preview first, so the single danger confirm can say
 *   up front whether positions will be closed ("Close & delete");
 * - feedback is toast-based (ok / error), no inline banners;
 * - after a delete the caller decides where to go — the detail page
 *   navigates back to the list via the router (no full page reload),
 *   the list page just reloads its data.
 */
import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import { clientApi, type StrategyRecord } from "./clientApi";
import {
  confirm as confirmDialog,
  prompt as promptDialog,
  toast,
} from "./dialogs";
import { describeCron, describeIntervalSeconds } from "./format";

type RefreshFn = () => Promise<void> | void;

// ---------------------------------------------------------------------------
// Shared display helpers (status labels + cron humanization)
// ---------------------------------------------------------------------------

export const KNOWN_STRATEGY_STATUSES = [
  "draft",
  "paper",
  "canary",
  "live",
  "paused",
  "archived",
  "static_review",
  "backtested",
] as const;

/** Localized strategy status label; unknown backend enums fall through raw. */
export function strategyStatusLabel(
  t: (key: string) => string,
  status: string,
): string {
  return (KNOWN_STRATEGY_STATUSES as readonly string[]).includes(status)
    ? t(`status.${status}`)
    : status;
}

/**
 * Human-readable cadence preview ("Every 5 min") for a cron expression
 * or fixed interval, or null when the shape isn't covered — callers
 * then fall back to showing the raw cron. Wired to the shared
 * ``cadence.*`` messages (same dictionary the workflows page uses).
 */
export function useCadenceHint(): (
  cron?: string | null,
  everySeconds?: number | null,
) => string | null {
  const t = useTranslations("cadence");
  return (cron, everySeconds) => {
    if (cron) {
      const desc = describeCron(cron);
      if (desc) {
        return t(desc.key, (desc.params ?? {}) as Record<string, string | number>);
      }
      return null;
    }
    if (everySeconds != null && everySeconds > 0) {
      const desc = describeIntervalSeconds(everySeconds);
      if (desc) {
        return t(desc.key, (desc.params ?? {}) as Record<string, string | number>);
      }
    }
    return null;
  };
}

export function useStrategyLifecycle(options?: {
  /** Called after a successful pause (or rename) so callers reload data. */
  onRefresh?: RefreshFn;
  /**
   * Called after a successful delete. Defaults to router.push("/strategies");
   * the list page passes its own reload instead.
   */
  onDeleted?: (strategy: StrategyRecord) => void;
}) {
  const t = useTranslations("strategies");
  const tCommon = useTranslations("common");
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);

  const fail = useCallback((e: unknown) => {
    toast({
      message: e instanceof Error ? e.message : String(e),
      tone: "error",
    });
  }, []);

  const rename = useCallback(
    async (strategy: StrategyRecord) => {
      const nextTitle = await promptDialog({
        title: t("editName"),
        message: t("renamePrompt", { id: strategy.id }),
        defaultValue: strategy.title || strategy.id,
        placeholder: t("fieldTitle"),
        okLabel: tCommon("save"),
      });
      if (nextTitle === null) return;
      const trimmed = nextTitle.trim();
      if (!trimmed) {
        toast({ message: t("nameRequired"), tone: "error" });
        return;
      }
      if (trimmed === (strategy.title || strategy.id)) return;
      setBusy(`rename:${strategy.id}`);
      try {
        const res = await clientApi.strategyUpdate(strategy.id, {
          title: trimmed,
          reason: "dashboard_rename_strategy",
        });
        if (!res.ok) throw new Error("strategy_rename_failed");
        toast({
          message: t("nameUpdated", { id: strategy.id, title: trimmed }),
          tone: "ok",
        });
        if (options?.onRefresh) await options.onRefresh();
      } catch (e) {
        fail(e);
      } finally {
        setBusy(null);
      }
    },
    [t, tCommon, options?.onRefresh, fail],
  );

  const pause = useCallback(
    async (strategy: StrategyRecord) => {
      if (strategy.status === "paused") {
        toast({ message: t("pausedInfo", { id: strategy.id }), tone: "ok" });
        return;
      }
      const ok = await confirmDialog({
        message: t("pauseConfirm", { id: strategy.id }),
        okLabel: t("pauseStrategy"),
        tone: "warning",
      });
      if (!ok) return;
      setBusy(`pause:${strategy.id}`);
      try {
        const res = await clientApi.strategySetStatus(
          strategy.id,
          "paused",
          "dashboard_pause",
        );
        if (!res.ok) throw new Error("strategy_pause_failed");
        toast({ message: t("pausedInfo", { id: strategy.id }), tone: "ok" });
        if (options?.onRefresh) await options.onRefresh();
      } catch (e) {
        fail(e);
      } finally {
        setBusy(null);
      }
    },
    [t, options?.onRefresh, fail],
  );

  const remove = useCallback(
    async (strategy: StrategyRecord) => {
      // One dry-run close-positions probe (side-effect free) so the
      // single danger confirm can state up front whether positions
      // will be closed — replaces the old confirm → close-or-pause →
      // preview-confirm nesting.
      let openCount = 0;
      let notional = "0.00";
      try {
        const preview = await clientApi.strategyClosePositions({
          strategy_id: strategy.id,
          dry_run: true,
        });
        if (preview.ok) {
          openCount = preview.count;
          notional = preview.positions
            .reduce((sum, row) => sum + (Number(row.notional_usd) || 0), 0)
            .toFixed(2);
        }
      } catch {
        // Preview is best-effort; plain delete confirm still applies.
      }
      const ok = await confirmDialog({
        message:
          openCount > 0
            ? t("deleteWithPositionsConfirm", {
                id: strategy.id,
                count: openCount,
                notional,
              })
            : t("deleteConfirm", { id: strategy.id }),
        tone: "danger",
        okLabel:
          openCount > 0 ? t("closeAndDelete") : tCommon("delete"),
      });
      if (!ok) return;
      setBusy(`delete:${strategy.id}`);
      try {
        if (openCount > 0) {
          const closed = await clientApi.strategyClosePositions({
            strategy_id: strategy.id,
            operator: "dashboard",
            reason: "strategy_delete_prepare",
          });
          if (!closed.ok) {
            throw new Error(closed.error || "strategy_close_positions_failed");
          }
        }
        const res = await clientApi.strategyDelete({
          strategy_id: strategy.id,
          force: false,
        });
        if (!res.ok) {
          // Backend refused (e.g. positions appeared between the
          // preview and the delete). Surface the state instead of
          // chaining more dialogs — the operator can pause first.
          throw new Error(
            res.state
              ? t("cannotDelete", {
                  positions: res.state.open_positions,
                  executors: res.state.active_executors,
                  orders: res.state.active_orders,
                })
              : res.error || "strategy_delete_failed",
          );
        }
        toast({ message: t("deletedInfo", { id: strategy.id }), tone: "ok" });
        if (options?.onDeleted) await options.onDeleted(strategy);
        else router.push("/strategies");
      } catch (e) {
        fail(e);
      } finally {
        setBusy(null);
      }
    },
    [t, tCommon, options?.onDeleted, router, fail],
  );

  return { busy, rename, pause, remove };
}
