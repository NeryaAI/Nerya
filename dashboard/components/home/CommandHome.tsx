"use client";

/**
 * CommandHome — the Codex-style landing surface.
 *
 * A centred "what should we build?" headline above a single prominent
 * composer. Submitting stashes the text as a compose draft, persists the
 * chosen model + permission mode into the shared chat run-settings, and
 * routes to ``/chat`` where ``ChatView`` drains the draft and auto-runs
 * the first turn. Suggestion rows seed the composer for one-tap starts.
 */

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import {
  buildChatModelOptions,
  loadRunSettings,
  saveRunSettings,
  type ChatModelOption,
  type ChatRunSettings,
  DEFAULT_CHAT_RUN_SETTINGS,
} from "../../lib/chat";
import { setComposeDraft, takeComposeDraft } from "../../lib/composeDraft";
import {
  PlusIcon,
  StrategiesIcon,
  AgentsIcon,
  GlobeIcon,
  SparkIcon,
} from "../icons";
import { ComposerModelMenu, ComposerPermissionMenu } from "../chat/ComposerRunControls";

export function CommandHome() {
  const router = useRouter();
  const t = useTranslations("commandHome");
  const tChat = useTranslations("chat");
  const [text, setText] = useState("");
  const [settings, setSettings] = useState<ChatRunSettings>(DEFAULT_CHAT_RUN_SETTINGS);
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    setSettings(loadRunSettings());
    const draft = takeComposeDraft();
    if (draft) setText(draft);
    const handle = window.setTimeout(() => taRef.current?.focus(), 30);
    let cancelled = false;
    void Promise.allSettled([
      clientApi.llmTiers(),
      clientApi.llmModels(),
      clientApi.llmConfig(),
    ])
      .then(([tiersResp, modelsResp, configResp]) => {
        if (cancelled) return;
        setModelOptions(
          buildChatModelOptions({
            tiers: tiersResp.status === "fulfilled" ? tiersResp.value : null,
            models: modelsResp.status === "fulfilled" ? modelsResp.value : null,
            config: configResp.status === "fulfilled" ? configResp.value : null,
          }),
        );
      })
      .catch(() => {
        /* runtime default model still works */
      });
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, []);

  const suggestions = useMemo(
    () => [
      { icon: StrategiesIcon, label: tChat("starterBtcScalpTitle"), prompt: tChat("starterBtcScalpPrompt") },
      { icon: AgentsIcon, label: tChat("starterNvdaTeamTitle"), prompt: tChat("starterNvdaTeamPrompt") },
      { icon: SparkIcon, label: tChat("starterCryptoStrategyTitle"), prompt: tChat("starterCryptoStrategyPrompt") },
      { icon: GlobeIcon, label: tChat("starterMacroNewsTitle"), prompt: tChat("starterMacroNewsPrompt") },
    ],
    [tChat],
  );
  function submit() {
    const value = text.trim();
    if (!value) return;
    saveRunSettings(settings);
    setComposeDraft(value);
    router.push("/chat");
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="command-home-root flex min-h-0 flex-1 flex-col items-center overflow-y-auto px-4 pb-14 pt-[92px] lg:px-5">
      <div className="w-full max-w-[760px]">
        <h1 className="text-center text-[34px] font-normal leading-[1.12] text-[color:var(--text-base)]">
          {t("title")}
        </h1>

        {/* Composer */}
        <div className="mt-9 overflow-hidden rounded-[18px] bg-[color:var(--card)] shadow-[0_10px_22px_rgba(0,0,0,0.14)]">
          <div className="rounded-[18px] border border-[color:var(--line)] bg-[color:var(--card-hi)] px-3.5 pb-2 pt-2.5 transition-colors focus-within:border-brand-500/55 sm:px-4">
            <textarea
              ref={taRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onKeyDown}
              rows={1}
              placeholder={t("placeholder")}
              className="block max-h-32 min-h-[28px] w-full resize-none bg-transparent text-[15px] leading-5 text-[color:var(--text-base)] placeholder:text-[color:var(--text-muted)] focus:outline-none"
            />
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                onClick={() => router.push("/chat")}
                className="inline-flex h-7 w-7 items-center justify-center rounded-full text-[color:var(--text-muted)] transition-colors hover:bg-white/5 hover:text-[color:var(--text-base)]"
                title={t("attach")}
                aria-label={t("attach")}
              >
                <PlusIcon size={15} />
              </button>

              <ComposerPermissionMenu
                settings={settings}
                onSettingsChange={setSettings}
                size="hero"
              />

              <div className="ml-auto flex min-w-0 items-center gap-1.5 max-[520px]:ml-0 max-[520px]:w-full max-[520px]:justify-end">
                <ComposerModelMenu
                  settings={settings}
                  onSettingsChange={setSettings}
                  modelOptions={modelOptions}
                  size="hero"
                />

                <button
                  type="button"
                  onClick={submit}
                  disabled={!text.trim()}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-brand-500 text-white transition-colors hover:bg-brand-400 disabled:cursor-not-allowed disabled:opacity-40"
                  title={t("send")}
                  aria-label={t("send")}
                >
                  <span className="translate-y-[-1px] text-[20px] leading-none">↑</span>
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Suggestions */}
        <div className="mt-6">
          <div>
            {suggestions.map((s, index) => {
              const Icon = s.icon;
              return (
                <button
                  key={s.label}
                  type="button"
                  onClick={() => {
                    setText(s.prompt);
                    taRef.current?.focus();
                  }}
                  className={`group flex h-10 w-full items-center gap-2.5 px-4 text-left text-[14px] text-[color:var(--text-muted)] transition-colors hover:bg-white/[0.025] hover:text-[color:var(--text-base)] ${
                    index > 0 ? "border-t border-white/[0.07]" : ""
                  }`}
                >
                  <Icon size={15} className="shrink-0 opacity-80 group-hover:opacity-100" />
                  <span className="flex-1 truncate">{s.label}</span>
                  <span className="shrink-0 text-[18px] text-brand-300/45 transition-colors group-hover:text-brand-300">
                    →
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

export default CommandHome;
