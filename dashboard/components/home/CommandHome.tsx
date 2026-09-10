"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import {
  buildChatModelOptions, loadRunSettings, saveRunSettings, DEFAULT_CHAT_RUN_SETTINGS,
  type ChatAttachment, type ChatModelOption, type ChatRunSettings,
} from "../../lib/chat";
import { setComposeDraftPayload, takeComposeDraftPayload } from "../../lib/composeDraft";
import { StrategiesIcon, AgentsIcon, GlobeIcon, SparkIcon } from "../icons";
import { ChatInput } from "../chat/ChatInput";

export function CommandHome() {
  const router = useRouter();
  const t = useTranslations("commandHome");
  const tChat = useTranslations("chat");
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [settings, setSettings] = useState<ChatRunSettings>(DEFAULT_CHAT_RUN_SETTINGS);
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const [navigating, setNavigating] = useState(false);
  const submitted = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    setSettings(loadRunSettings());
    const draft = takeComposeDraftPayload();
    if (draft) { setText(draft.text); setAttachments(draft.attachments); }
    // Do not summon the on-screen keyboard as soon as a phone opens home.
    const focus = window.setTimeout(() => {
      if (window.matchMedia("(pointer: fine)").matches) inputRef.current?.focus();
    }, 30);
    let cancelled = false;
    void Promise.allSettled([clientApi.llmTiers(), clientApi.llmModels(), clientApi.llmConfig()])
      .then(([tiers, models, config]) => {
        if (!cancelled) setModelOptions(buildChatModelOptions({
          tiers: tiers.status === "fulfilled" ? tiers.value : null,
          models: models.status === "fulfilled" ? models.value : null,
          config: config.status === "fulfilled" ? config.value : null,
        }));
      });
    return () => { cancelled = true; window.clearTimeout(focus); };
  }, []);

  const suggestions = useMemo(() => [
    { icon: StrategiesIcon, label: tChat("starterBtcScalpTitle"), prompt: tChat("starterBtcScalpPrompt") },
    { icon: AgentsIcon, label: tChat("starterNvdaTeamTitle"), prompt: tChat("starterNvdaTeamPrompt") },
    { icon: SparkIcon, label: tChat("starterCryptoStrategyTitle"), prompt: tChat("starterCryptoStrategyPrompt") },
    { icon: GlobeIcon, label: tChat("starterMacroNewsTitle"), prompt: tChat("starterMacroNewsPrompt") },
  ], [tChat]);

  function submit() {
    if (submitted.current || (!text.trim() && !attachments.length)) return;
    submitted.current = true;
    setNavigating(true);
    saveRunSettings(settings);
    setComposeDraftPayload({ text: text.trim(), attachments, autoSend: true });
    router.push("/chat");
  }

  return (
    <div className="command-home-root flex min-h-0 flex-1 flex-col items-center overflow-y-auto px-4 py-10 sm:pt-20 lg:px-5">
      <div className="w-full max-w-[760px]">
        <h1 className="text-balance text-center text-[28px] font-medium leading-tight text-[color:var(--text-base)] sm:text-[34px]">{t("title")}</h1>
        <div className="mt-7 sm:mt-9">
          <ChatInput variant="hero" inputRef={inputRef} value={text} onChange={setText} onSend={submit}
            sending={navigating} locked={navigating} placeholder={t("placeholder")}
            settings={settings} onSettingsChange={setSettings} modelOptions={modelOptions}
            attachments={attachments} onAttachmentsChange={setAttachments} />
        </div>
        <div className="mt-6 divide-y divide-[color:var(--line)]">
          {suggestions.map((suggestion) => {
            const Icon = suggestion.icon;
            return <button key={suggestion.label} type="button" disabled={navigating}
              onClick={() => { setText(suggestion.prompt); inputRef.current?.focus(); }}
              className="group flex min-h-11 w-full items-center gap-3 rounded-md px-3 py-3 text-left text-sm text-[color:var(--text-muted)] transition-colors hover:bg-brand-500/5 hover:text-[color:var(--text-base)]">
              <Icon size={16} className="shrink-0" />
              <span className="min-w-0 flex-1">{suggestion.label}</span>
              <span aria-hidden className="shrink-0">→</span>
            </button>;
          })}
        </div>
      </div>
    </div>
  );
}
export default CommandHome;
