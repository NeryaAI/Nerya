"use client";

import { useTranslations } from "next-intl";
import type { ChatThread } from "../../lib/chat";
import { PlusIcon, TrashIcon } from "../icons";

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
  onPick,
  onNew,
  onDelete,
}: {
  threads: ChatThread[];
  activeId: string | null;
  onPick: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}) {
  const t = useTranslations("chat");
  const tCommon = useTranslations("common");
  return (
    <aside className="w-64 shrink-0 border-r backdrop-blur-glass flex flex-col" style={{ background: "var(--panel-bg)", borderColor: "var(--line)" }}>
      <div className="p-3 border-b" style={{ borderColor: "var(--line)" }}>
        <button
          onClick={onNew}
          className="w-full text-left rounded-lg border border-brand-500/30 bg-gradient-to-r from-brand-500/15 to-fluid-500/10 hover:from-brand-500/25 hover:to-fluid-500/15 text-white text-sm px-3 py-2 transition-colors flex items-center gap-2"
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
              return (
                <li key={th.id}>
                  <div
                    className={`group flex items-start gap-1 rounded-lg px-2 py-2 text-sm transition-colors cursor-pointer ${
                      active
                        ? "bg-gradient-to-r from-brand-500/20 via-brand-500/10 to-transparent border border-brand-500/30 text-white shadow-[inset_2px_0_0_#b48bff]"
                        : "hover:bg-white/[0.04] border border-transparent text-ink-200"
                    }`}
                  >
                    <button
                      onClick={() => onPick(th.id)}
                      className="flex-1 text-left min-w-0"
                    >
                      <div className="truncate text-[13px]">{th.title}</div>
                      <div className="text-[10px] text-ink-500 font-mono mt-0.5">
                        {t("messages", { count: th.messages.length })} · {timeAgo(th.updated_ts)}
                      </div>
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm(t("deleteConfirm"))) onDelete(th.id);
                      }}
                      className="opacity-0 group-hover:opacity-100 text-ink-500 hover:text-[#ef5564] text-xs px-1.5 transition-opacity"
                      title={tCommon("delete")}
                      aria-label={tCommon("delete")}
                    >
                      <TrashIcon size={14} />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="p-3 border-t text-[10px] text-ink-500 leading-relaxed" style={{ borderColor: "var(--line)" }}>
        {t("historyFooter")}
      </div>
    </aside>
  );
}
