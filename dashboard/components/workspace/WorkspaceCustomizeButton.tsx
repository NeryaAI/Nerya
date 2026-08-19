"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { setComposeDraft } from "../../lib/composeDraft";
import {
  AgentsIcon,
  OverviewIcon,
  SettingsIcon,
  SkillsIcon,
  SparkIcon,
  PuzzleIcon,
  XIcon,
} from "../icons";

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
  hint: string;
  prompt: string;
  Icon: typeof PuzzleIcon;
};

/**
 * A small, repeatable hand-off from any UI surface to the Agent Workspace.
 *
 * The button deliberately does not mutate the manifest itself.  It gives the
 * agent enough context to choose the audited high-level customization tool,
 * render a structured result in chat, and keep every persistent mutation behind
 * the existing operator approval boundary.
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
        hint: t("presetWidgetHint"),
        prompt: t("presetWidgetPrompt"),
        Icon: PuzzleIcon,
      },
      {
        id: "page",
        label: t("presetPage"),
        hint: t("presetPageHint"),
        prompt: t("presetPagePrompt"),
        Icon: OverviewIcon,
      },
      {
        id: "skill",
        label: t("presetSkill"),
        hint: t("presetSkillHint"),
        prompt: t("presetSkillPrompt"),
        Icon: SkillsIcon,
      },
      {
        id: "agent",
        label: t("presetAgent"),
        hint: t("presetAgentHint"),
        prompt: t("presetAgentPrompt"),
        Icon: AgentsIcon,
      },
      {
        id: "config",
        label: t("presetConfig"),
        hint: t("presetConfigHint"),
        prompt: t("presetConfigPrompt"),
        Icon: SettingsIcon,
      },
    ],
    [t],
  );

  const scope =
    context === "home"
      ? t("homeScope")
      : t("pageScope", { title: context.title || context.pageId });

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  function beginAgentTurn() {
    const trimmed = request.trim();
    if (!trimmed) return;
    const prompt = [
      t("agentPreamble"),
      `${t("scopeLabel")}: ${scope}`,
      "",
      `${t("requestLabel")}: ${trimmed}`,
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
        <SparkIcon size={14} aria-hidden />
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
            aria-describedby="workspace-customize-description"
            className="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-[620px] flex-col overflow-hidden rounded-xl border border-[color:var(--line)] bg-[color:var(--card-hi)] shadow-2xl"
          >
            <div className="flex items-start justify-between gap-4 border-b border-[color:var(--line)] px-4 py-3.5">
              <div>
                <h2 id="workspace-customize-title" className="text-[15px] font-medium text-[color:var(--text-base)]">
                  {t("customizeTitle")}
                </h2>
                <p id="workspace-customize-description" className="mt-1 text-[12px] leading-relaxed text-[color:var(--text-muted)]">
                  {t("customizeDescription", { scope })}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="shrink-0 rounded-md p-1.5 text-[color:var(--text-muted)] hover:bg-white/5 hover:text-[color:var(--text-base)]"
                aria-label={t("close")}
              >
                <XIcon size={16} />
              </button>
            </div>

            <div className="min-h-0 space-y-3 overflow-y-auto px-4 py-4">
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {presets.map((preset) => (
                  <button
                    key={preset.id}
                    type="button"
                    onClick={() => setRequest(preset.prompt)}
                    className="group flex min-h-[62px] items-start gap-2.5 rounded-lg border border-[color:var(--line)] bg-white/[0.015] px-3 py-2.5 text-left transition-colors hover:border-brand-500/45 hover:bg-brand-500/[0.055]"
                  >
                    <preset.Icon size={16} className="mt-0.5 shrink-0 text-brand-300" />
                    <span className="min-w-0">
                      <span className="block text-[12px] font-medium text-[color:var(--text-base)] group-hover:text-brand-100">
                        {preset.label}
                      </span>
                      <span className="mt-0.5 block text-[10.5px] leading-relaxed text-[color:var(--text-muted)]">
                        {preset.hint}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
              <label htmlFor="workspace-customize-request" className="sr-only">
                {t("requestLabel")}
              </label>
              <textarea
                id="workspace-customize-request"
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

