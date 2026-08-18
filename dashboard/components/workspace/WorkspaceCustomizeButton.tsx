"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { setComposeDraft } from "../../lib/composeDraft";

export type WorkspaceCustomizeContext =
  | "home"
  | { pageId: string; title?: string };

type Props = {
  context?: WorkspaceCustomizeContext;
  compact?: boolean;
  className?: string;
};

type PromptPreset = {
  id: string;
  label: string;
  prompt: string;
};

/**
 * A small, repeatable hand-off from any UI surface to the Agent Workspace.
 *
 * The button deliberately does not mutate the manifest itself.  It gives the
 * agent enough context to inspect `ui/workspace.yml`, produce a structured
 * `core_config_patch` proposal, and let the operator approve it in Inbox.
 */
export function WorkspaceCustomizeButton({
  context = "home",
  compact = false,
  className = "",
}: Props) {
  const router = useRouter();
  const t = useTranslations("workspaceUi");
  const [open, setOpen] = useState(false);
  const [request, setRequest] = useState("");

  const presets = useMemo<PromptPreset[]>(
    () => [
      {
        id: "widget",
        label: t("presetWidget"),
        prompt: t("presetWidgetPrompt"),
      },
      {
        id: "page",
        label: t("presetPage"),
        prompt: t("presetPagePrompt"),
      },
      {
        id: "runtime",
        label: t("presetRuntime"),
        prompt: t("presetRuntimePrompt"),
      },
    ],
    [t],
  );

  const scope =
    context === "home"
      ? t("homeScope")
      : t("pageScope", { title: context.title || context.pageId });

  function beginAgentTurn() {
    const trimmed = request.trim();
    if (!trimmed) return;
    const prompt = [
      t("agentPreamble"),
      `Scope: ${scope}`,
      "",
      `User request: ${trimmed}`,
      "",
      t("agentRules"),
    ].join("\n");
    setComposeDraft(prompt);
    setOpen(false);
    router.push("/chat");
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={[
          "inline-flex items-center justify-center gap-1.5 rounded-md border border-brand-500/35 text-brand-200 transition-colors hover:border-brand-400/60 hover:bg-brand-500/10",
          compact ? "h-7 px-2 text-[12px]" : "h-8 px-3 text-[12px]",
          className,
        ].join(" ")}
      >
        <span aria-hidden className="text-[14px] leading-none">✦</span>
        <span>{t("customize")}</span>
      </button>

      {open ? (
        <div
          className="fixed inset-0 z-[100] flex items-end justify-center bg-black/55 p-3 sm:items-center"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setOpen(false);
          }}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="workspace-customize-title"
            className="w-full max-w-[620px] overflow-hidden rounded-xl border border-[color:var(--line)] bg-[color:var(--card-hi)] shadow-2xl"
          >
            <div className="flex items-start justify-between gap-4 border-b border-[color:var(--line)] px-4 py-3.5">
              <div>
                <h2 id="workspace-customize-title" className="text-[15px] font-medium text-[color:var(--text-base)]">
                  {t("customizeTitle")}
                </h2>
                <p className="mt-1 text-[12px] leading-relaxed text-[color:var(--text-muted)]">
                  {t("customizeDescription", { scope })}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="shrink-0 rounded-md px-2 py-1 text-[color:var(--text-muted)] hover:bg-white/5 hover:text-[color:var(--text-base)]"
                aria-label={t("close")}
              >
                ×
              </button>
            </div>

            <div className="space-y-3 px-4 py-4">
              <div className="flex flex-wrap gap-1.5">
                {presets.map((preset) => (
                  <button
                    key={preset.id}
                    type="button"
                    onClick={() => setRequest(preset.prompt)}
                    className="rounded-full border border-[color:var(--line)] px-2.5 py-1 text-[11px] text-[color:var(--text-muted)] hover:border-brand-500/45 hover:text-brand-200"
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
              <textarea
                autoFocus
                value={request}
                onChange={(event) => setRequest(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                    event.preventDefault();
                    beginAgentTurn();
                  }
                }}
                rows={4}
                placeholder={t("customizePlaceholder")}
                className="w-full resize-y rounded-lg border border-[color:var(--line)] bg-[color:var(--panel-bg)] px-3 py-2.5 text-[13px] leading-relaxed text-[color:var(--text-base)] outline-none placeholder:text-[color:var(--text-muted)] focus:border-brand-500/55"
              />
              <div className="flex items-center justify-between gap-3">
                <p className="text-[11px] leading-relaxed text-[color:var(--text-muted)]">
                  {t("approvalHint")}
                </p>
                <button
                  type="button"
                  onClick={beginAgentTurn}
                  disabled={!request.trim()}
                  className="shrink-0 rounded-md bg-brand-500 px-3 py-2 text-[12px] font-medium text-white transition-colors hover:bg-brand-400 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {t("askAgent")}
                </button>
              </div>
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}

