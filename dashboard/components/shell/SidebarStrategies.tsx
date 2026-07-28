"use client";

/**
 * SidebarStrategies — running strategies pinned above the CHATS rail.
 *
 * Layout (second level indented under each strategy):
 *
 *   STRATEGIES
 *   ◔ btc-scalper        ● live   [+]
 *     ▭ position review
 *     ▭ risk chat
 *     ↑ evolution · tuning run     ── evolution flows group below
 *   ◔ nvda-trend         ● paper  [+]
 *
 * First-level rows use the strategy icon (StrategiesIcon) so they read
 * differently from chat threads (MessagesIcon). Expanding a strategy
 * fetches the backend sessions bound to that ``strategy_id``
 * (``GET /agent/sessions?strategy_id=…&include=all``) and merges any
 * locally-created threads that carry the same ``strategy_id``.
 * Evolution / tuning sessions render below the regular sub-sessions so
 * the strategy's self-evolution trail stays attached to its strategy.
 *
 * The [+] button opens ``/chat?strategy=<id>`` — ChatView stamps the
 * ``strategy_id`` onto the new thread and forwards it on every
 * ``run_turn``, which binds the backend session to the strategy and
 * makes the kernel inject the full strategy file context.
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  clientApi,
  type AgentSession,
  type StrategyRecord,
} from "../../lib/clientApi";
import {
  loadThreads,
  loadDeletedSessionIds,
  subscribeThreadsChanged,
  deleteThreadLocally,
} from "../../lib/chat";
import { confirm as confirmDialog } from "../../lib/dialogs";
import {
  ChevronDownIcon,
  EvolutionIcon,
  MessagesIcon,
  PlusIcon,
  StrategiesIcon,
  TrashIcon,
} from "../icons";

const OPEN_KEY = "nerya.sidebar.strategies-open";
/** Deployed = the operator considers it "running"; drafts and archived
 * strategies stay on the strategies page only. */
const RUNNING_STATUSES = new Set(["live", "canary", "paper"]);
const REFRESH_MS = 60_000;
const SESSION_LIMIT = 20;

type StrategySession = {
  id: string;
  title: string;
  updatedTs: number;
  evolution: boolean;
};

function parseTs(iso?: string | null): number {
  if (!iso) return 0;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : 0;
}

/** Evolution / tuning flows are regular strategy-bound sessions whose
 * ``source`` marks the self-evolution pipeline (``strategy_evolution``,
 * tuning runs, …). */
function isEvolutionSource(source: string): boolean {
  const s = source.toLowerCase();
  return s.includes("evolution") || s.includes("tuning");
}

function sessionEntry(s: AgentSession): StrategySession | null {
  const id = String(s.session_id || "").trim();
  if (!id) return null;
  const metaTitle =
    typeof s.meta?.title === "string" ? s.meta.title.trim() : "";
  return {
    id,
    title: metaTitle || `Session ${id.slice(0, 8)}`,
    updatedTs: parseTs(s.updated_at) || parseTs(s.created_at),
    evolution: isEvolutionSource(String(s.source || "")),
  };
}

function statusDotClass(status: string): string {
  if (status === "live") return "bg-accent-400";
  if (status === "paper" || status === "canary") return "bg-brand-400";
  return "bg-ink-500";
}

function loadOpenIds(): Set<string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(OPEN_KEY) || "[]");
    return new Set(
      Array.isArray(parsed)
        ? parsed.filter((v): v is string => typeof v === "string")
        : [],
    );
  } catch {
    return new Set();
  }
}

function saveOpenIds(ids: Set<string>) {
  try {
    localStorage.setItem(OPEN_KEY, JSON.stringify(Array.from(ids)));
  } catch {
    /* ignore */
  }
}

export function SidebarStrategies() {
  const pathname = usePathname() || "/";
  const router = useRouter();
  const t = useTranslations("sidebar");
  const tChat = useTranslations("chat");
  const tCommon = useTranslations("common");

  const [strategies, setStrategies] = useState<StrategyRecord[]>([]);
  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set());
  const [sessionsByStrategy, setSessionsByStrategy] = useState<
    Record<string, StrategySession[]>
  >({});
  const [loadingIds, setLoadingIds] = useState<Set<string>>(() => new Set());
  const openIdsRef = useRef(openIds);
  openIdsRef.current = openIds;

  useEffect(() => {
    setOpenIds(loadOpenIds());
  }, []);

  const refreshStrategies = useCallback(async () => {
    try {
      const res = await clientApi.strategiesAll(false);
      setStrategies(
        (res.strategies ?? []).filter((s) => RUNNING_STATUSES.has(s.status)),
      );
    } catch {
      // Backend may be down; keep whatever we last rendered.
    }
  }, []);

  const refreshSessions = useCallback(async (strategyId: string) => {
    setLoadingIds((prev) => new Set(prev).add(strategyId));
    let backend: StrategySession[] = [];
    try {
      const res = await clientApi.sessionList(strategyId, SESSION_LIMIT, {
        include: "all",
      });
      backend = (res.sessions ?? [])
        .map(sessionEntry)
        .filter((s): s is StrategySession => s !== null);
    } catch {
      backend = [];
    }
    // Locally-deleted sessions keep their tombstone until the backend
    // delete lands, so a refresh can't resurrect them in the rail.
    const deleted = loadDeletedSessionIds();
    const byId = new Map(
      backend.filter((s) => !deleted.has(s.id)).map((s) => [s.id, s]),
    );
    // Merge locally-created threads (a fresh "+" chat has no backend
    // session until its first turn runs) so it shows up immediately.
    for (const thread of loadThreads()) {
      if (thread.strategy_id !== strategyId) continue;
      const existing = byId.get(thread.id);
      if (existing) {
        byId.set(thread.id, {
          ...existing,
          title: existing.title.startsWith("Session ")
            ? thread.title || existing.title
            : existing.title,
          updatedTs: Math.max(existing.updatedTs, thread.updated_ts || 0),
        });
      } else {
        byId.set(thread.id, {
          id: thread.id,
          title: thread.title || t("untitledChat"),
          updatedTs: thread.updated_ts || 0,
          evolution: false,
        });
      }
    }
    setSessionsByStrategy((prev) => ({
      ...prev,
      [strategyId]: Array.from(byId.values()).sort(
        (a, b) => b.updatedTs - a.updatedTs,
      ),
    }));
    setLoadingIds((prev) => {
      const next = new Set(prev);
      next.delete(strategyId);
      return next;
    });
  }, [t]);

  // Strategy roster: load on mount, then refresh on a slow interval.
  useEffect(() => {
    void refreshStrategies();
    const timer = setInterval(() => void refreshStrategies(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [refreshStrategies]);

  // Sub-sessions: (re)load whenever an expanded strategy appears and
  // whenever local threads change (new "+" chat, rename, delete).
  useEffect(() => {
    for (const s of strategies) {
      if (openIds.has(s.id)) void refreshSessions(s.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategies, openIds]);

  useEffect(() => {
    return subscribeThreadsChanged(() => {
      for (const id of openIdsRef.current) void refreshSessions(id);
    });
  }, [refreshSessions]);

  function toggle(strategyId: string) {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(strategyId)) next.delete(strategyId);
      else next.add(strategyId);
      saveOpenIds(next);
      return next;
    });
  }

  function newStrategyChat(strategyId: string) {
    router.push(`/chat?strategy=${encodeURIComponent(strategyId)}`);
  }

  async function removeSession(strategyId: string, sessionId: string) {
    const ok = await confirmDialog({
      message: tChat("deleteConfirm"),
      tone: "danger",
    });
    if (!ok) return;
    deleteThreadLocally(sessionId); // tombstone + broadcast
    setSessionsByStrategy((prev) => ({
      ...prev,
      [strategyId]: (prev[strategyId] ?? []).filter((s) => s.id !== sessionId),
    }));
    void clientApi.sessionDelete(sessionId).catch(() => {
      /* keep the local tombstone even if the backend delete fails */
    });
  }

  if (strategies.length === 0) return null;

  return (
    <div className="shrink-0 pb-2">
      <div className="px-3 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wide text-[color:var(--text-muted)]">
        {t("sectionStrategies")}
      </div>
      <div className="space-y-0.5">
        {strategies.map((s) => {
          const open = openIds.has(s.id);
          const sessions = sessionsByStrategy[s.id] ?? [];
          const regular = sessions.filter((x) => !x.evolution);
          const evolution = sessions.filter((x) => x.evolution);
          const strategyActive = pathname.startsWith(`/strategies/${s.id}`);
          return (
            <div key={s.id}>
              <div
                className={`group sidebar-item pr-1 ${strategyActive ? "sidebar-item-active" : "sidebar-item-idle"}`}
              >
                <button
                  type="button"
                  onClick={() => toggle(s.id)}
                  className="shrink-0 rounded p-0.5 text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]"
                  title={open ? t("collapse") : t("expand")}
                  aria-label={open ? t("collapse") : t("expand")}
                  aria-expanded={open}
                >
                  <ChevronDownIcon
                    size={13}
                    className={`transition-transform ${open ? "" : "-rotate-90"}`}
                  />
                </button>
                <Link
                  href={`/strategies/${encodeURIComponent(s.id)}`}
                  className="flex min-w-0 flex-1 items-center gap-2"
                  title={s.title || s.id}
                >
                  <StrategiesIcon
                    size={14}
                    className={`shrink-0 ${strategyActive ? "text-brand-200" : "text-[color:var(--text-muted)]"}`}
                  />
                  <span className="truncate">{s.title || s.id}</span>
                  <span
                    className={`ml-auto h-1.5 w-1.5 shrink-0 rounded-full ${statusDotClass(s.status)}`}
                    title={s.status}
                  />
                </Link>
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    newStrategyChat(s.id);
                  }}
                  className="ml-1 shrink-0 rounded p-1 text-[color:var(--text-muted)] opacity-0 transition-opacity hover:text-brand-200 group-hover:opacity-100"
                  title={t("newStrategyChat")}
                  aria-label={t("newStrategyChat")}
                >
                  <PlusIcon size={13} />
                </button>
              </div>

              {open ? (
                <div className="ml-4 space-y-0.5 border-l pl-1.5" style={{ borderColor: "var(--line)" }}>
                  {sessions.length === 0 ? (
                    <div className="px-2 py-1 text-[11px] italic text-[color:var(--text-muted)]">
                      {loadingIds.has(s.id)
                        ? tCommon("loading")
                        : t("noStrategySessions")}
                    </div>
                  ) : null}
                  {regular.map((sess) => (
                    <StrategySessionRow
                      key={sess.id}
                      session={sess}
                      active={pathname.startsWith(`/chat/${sess.id}`)}
                      onDelete={() => void removeSession(s.id, sess.id)}
                      deleteLabel={tCommon("delete")}
                    />
                  ))}
                  {evolution.length > 0 ? (
                    <div className="px-2 pt-1 text-[10px] font-medium uppercase tracking-wide text-[color:var(--text-muted)]">
                      {t("strategyEvolution")}
                    </div>
                  ) : null}
                  {evolution.map((sess) => (
                    <StrategySessionRow
                      key={sess.id}
                      session={sess}
                      active={pathname.startsWith(`/chat/${sess.id}`)}
                      onDelete={() => void removeSession(s.id, sess.id)}
                      deleteLabel={tCommon("delete")}
                    />
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StrategySessionRow({
  session,
  active,
  onDelete,
  deleteLabel,
}: {
  session: StrategySession;
  active: boolean;
  onDelete: () => void;
  deleteLabel: string;
}) {
  const Icon = session.evolution ? EvolutionIcon : MessagesIcon;
  return (
    <div
      className={`group sidebar-item pr-1 ${active ? "sidebar-item-active" : "sidebar-item-idle"}`}
    >
      <Link
        href={`/chat/${encodeURIComponent(session.id)}`}
        className="flex min-w-0 flex-1 items-center gap-2"
        title={session.title}
      >
        <Icon
          size={13}
          className={`shrink-0 ${
            active
              ? "text-brand-200"
              : session.evolution
              ? "text-fluid-400"
              : "text-[color:var(--text-muted)]"
          }`}
        />
        <span className="truncate text-[12px]">{session.title}</span>
      </Link>
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onDelete();
        }}
        className="ml-1 shrink-0 rounded p-1 text-[color:var(--text-muted)] opacity-0 transition-opacity hover:text-rose-400 group-hover:opacity-100"
        title={deleteLabel}
        aria-label={deleteLabel}
      >
        <TrashIcon size={12} />
      </button>
    </div>
  );
}

export default SidebarStrategies;
