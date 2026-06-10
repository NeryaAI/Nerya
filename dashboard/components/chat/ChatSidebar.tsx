"use client";

import { useTranslations } from "next-intl";
import type { ChatThread } from "../../lib/chat";
import { PlusIcon, TrashIcon } from "../icons";
import { confirm as confirmDialog } from "../../lib/dialogs";

function timeAgo(ts: number): string {
  const delta = Date.now() - ts;
  const s = Math.floor(delta / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d`;
}

export function ChatSidebar({
  threads,
  activeId,
  hasMore,
  loadingMore,
  onPick,
  onNew,
  onDelete,
  onLoadMore,
}: {
  threads: ChatThread[];
  activeId: string | null;
  hasMore?: boolean;
  loadingMore?: boolean;
  onPick: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onLoadMore?: () => void;
}) {
  const t = useTranslations("chat");
  const tCommon = useTranslations("common");
  return (
    <aside className="flex h-[168px] w-full shrink-0 flex-col border-b backdrop-blur-airy md:h-auto md:w-64 md:border-b-0 md:border-r" style={{ background: "var(--panel-bg)", borderColor: "var(--line)" }}>
      <div
        className="px-3.5 pt-3 pb-2 flex items-center justify-between"
      >
        <span className="text-[12px] font-medium text-ink-400">
          {t("history")}
        </span>
        <span className="text-[10px] font-mono text-ink-500">
          {threads.length}
        </span>
      </div>
      <div className="px-3 pb-3">
        <button
          onClick={onNew}
          className="w-full cursor-pointer text-left rounded-xl border border-brand-500/25 bg-brand-500/10 hover:bg-brand-500/15 hover:border-brand-500/40 text-white text-sm px-3 py-2 transition-colors flex items-center gap-2"
          title={t("newChat")}
        >
          <PlusIcon size={15} className="text-brand-200" />
          <span>{t("newChat")}</span>
        </button>
      </div>
      <div className="embedded-scroll flex-1">
        {threads.length === 0 ? (
          <div className="p-4 text-xs text-ink-500 italic">
            {t("emptyHistory")}
          </div>
        ) : (
          <ul className="p-2 space-y-1">
            {threads.map((th) => {
              const active = th.id === activeId;
              const messageCount = th.messages.length || th.message_count || 0;
              return (
                <li key={th.id}>
                  <div
                    className={`group flex items-start gap-1 rounded-lg px-2 py-2 text-sm transition-colors cursor-pointer ${
                      active
                        ? "bg-brand-500/15 border border-brand-500/40 text-white font-semibold"
                        : "hover:bg-brand-500/5 border border-transparent text-ink-200"
                    }`}
                  >
                    <button
                      onClick={() => onPick(th.id)}
                      className="flex-1 text-left min-w-0"
                    >
                      <div className="truncate text-[13px]">{th.title}</div>
                      <div className="text-[10px] text-ink-500 font-mono mt-0.5">
                        {t("messages", { count: messageCount })} · {timeAgo(th.updated_ts)}
                      </div>
                    </button>
                    <button
                      onClick={async (e) => {
                        e.stopPropagation();
                        const ok = await confirmDialog({
                          message: t("deleteConfirm"),
                          tone: "danger",
                        });
                        if (ok) onDelete(th.id);
                      }}
                      className="opacity-0 group-hover:opacity-100 text-ink-500 hover:text-danger text-xs px-1.5 transition-opacity"
                      title={tCommon("delete")}
                      aria-label={tCommon("delete")}
                    >
                      <TrashIcon size={14} />
                    </button>
                  </div>
                </li>
              );
            })}
            {hasMore ? (
              <li className="pt-1">
                <button
                  onClick={onLoadMore}
                  disabled={loadingMore}
                  className="w-full rounded-lg border border-white/10 px-2 py-2 text-xs text-ink-300 hover:text-white hover:bg-white/[0.04] disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {loadingMore ? t("loadingHistory") : t("loadMoreHistory")}
                </button>
              </li>
            ) : null}
          </ul>
        )}
      </div>
      <div className="hidden border-t p-3 text-[10px] leading-relaxed text-ink-500 md:block" style={{ borderColor: "var(--line)" }}>
        {t("historyFooter")}
      </div>
    </aside>
  );
}
