"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import {
  buildChatModelOptions, loadRunSettings, saveRunSettings, DEFAULT_CHAT_RUN_SETTINGS,
  type ChatModelOption, type ChatRunSettings,
} from "../../lib/chat";
import { setWorkspaceComposeDraft, takeWorkspaceComposeDraft } from "../../lib/workspaceComposeDraft";
import { AgentStart } from "../chat/AgentStart";
import { ChatInput } from "../chat/ChatInput";
import { useWorkbench } from "../chat/useWorkbench";
import { RuntimeNotice } from "../chat/RuntimeNotice";
import { useChatDraft } from "../chat/useChatDraft";

export function CommandHome() {
  const router = useRouter();
  const t = useTranslations("commandHome");
  const zh = useLocale().startsWith("zh");
  const draft=useChatDraft("home");
  const { text, setText, attachments, setAttachments } = draft;
  const workbench=useWorkbench();
  const [settings, setSettings] = useState<ChatRunSettings>(DEFAULT_CHAT_RUN_SETTINGS);
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const [navigating, setNavigating] = useState(false);
  const submitted = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
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
  useEffect(() => {
    if (!draft.ready) return;
    const handoff = takeWorkspaceComposeDraft();
    if (handoff) draft.replace(handoff);
  }, [draft.ready, draft.workspaceId]);
  useEffect(() => {
    submitted.current = false; setNavigating(false);
    setSettings(draft.workspaceId ? loadRunSettings() : DEFAULT_CHAT_RUN_SETTINGS);
  }, [draft.workspaceId]);

  useEffect(()=>{if(draft.settings)setSettings(draft.settings);},[draft.settings]);
  function submit() {
    if (!draft.ready || workbench.connection!=="online" || submitted.current || (!text.trim() && !attachments.length)) return;
    const receipt = draft.capture();
    if (!setWorkspaceComposeDraft({ text: text.trim(), attachments, settings, autoSend: true })) return;
    submitted.current = true;
    setNavigating(true);
    saveRunSettings(settings);
    draft.clearIfUnchanged(receipt);
    router.push("/chat");
  }

  return <div className="command-home-root flex min-h-0 flex-1 flex-col">
    <RuntimeNotice workbench={workbench}/>
    <AgentStart value={text} onChange={setText} disabled={navigating} composer={
      <ChatInput variant="hero" inputRef={inputRef} value={text} onChange={setText} onSend={submit}
        sending={navigating} locked={!draft.ready||navigating||workbench.connection!=="online"} draftLocked={navigating} placeholder={t("placeholder")}
        lockMessage={navigating ? (i18nCopy(zh, "copy.components_home_CommandHome.001")) : (i18nCopy(zh, "copy.components_home_CommandHome.002"))}
        settings={settings} onSettingsChange={next=>{setSettings(next);draft.setSettings(next);}} modelOptions={modelOptions}
        attachments={attachments} onAttachmentsChange={setAttachments} />
    } />
  </div>;
}
export default CommandHome;
