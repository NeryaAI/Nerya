"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import {
  buildChatModelOptions, loadRunSettings, saveRunSettings, DEFAULT_CHAT_RUN_SETTINGS,
  type ChatAttachment, type ChatModelOption, type ChatRunSettings,
} from "../../lib/chat";
import { setComposeDraftPayload, takeComposeDraftPayload } from "../../lib/composeDraft";
import { AgentStart } from "../chat/AgentStart";
import { ChatInput } from "../chat/ChatInput";

export function CommandHome() {
  const router = useRouter();
  const t = useTranslations("commandHome");
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

  function submit() {
    if (submitted.current || (!text.trim() && !attachments.length)) return;
    submitted.current = true;
    setNavigating(true);
    saveRunSettings(settings);
    setComposeDraftPayload({ text: text.trim(), attachments, autoSend: true });
    router.push("/chat");
  }

  return <div className="command-home-root flex min-h-0 flex-1 flex-col">
    <AgentStart value={text} onChange={setText} disabled={navigating} composer={
      <ChatInput variant="hero" inputRef={inputRef} value={text} onChange={setText} onSend={submit}
        sending={navigating} locked={navigating} placeholder={t("placeholder")}
        settings={settings} onSettingsChange={setSettings} modelOptions={modelOptions}
        attachments={attachments} onAttachmentsChange={setAttachments} />
    } />
  </div>;
}
export default CommandHome;
