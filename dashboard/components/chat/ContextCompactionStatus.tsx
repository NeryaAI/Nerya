"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import type { LiveEvent } from "../../lib/chat";

export function ContextCompactionStatus({events}:{events:LiveEvent[]}) {
  const zh=useLocale().startsWith("zh");
  const event=events.findLast(item=>["compact.start","compact.complete","context.degraded"].includes(item.kind));
  if(!event)return null;
  const running=event.kind==="compact.start", degraded=event.kind==="context.degraded"||event.preservation_status==="failed"||event.status==="context_degraded";
  const label=degraded?(i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.001")):
    running?(i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.002")):
    event.status==="applied"?(i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.003")):(i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.004"));
  const causes:Record<string,string>={message_count:i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.005"),token_pressure:i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.006"),context_overflow:i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.007"),checkpoint_conflict:i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.008")};
  return <details className="mb-3 rounded border border-[color:var(--line)] px-3 py-2 text-xs" data-testid="context-compaction-status"><summary className={degraded?"cursor-pointer text-warn":"cursor-pointer text-[color:var(--text-muted)]"}>{label}</summary><div className="mt-2 space-y-1 text-[color:var(--text-muted)]">
    <p>{causes[String(event.cause||event.reason)]||String(event.cause||event.reason||"")}</p>
    {typeof event.before_message_count==="number"&&<p>{i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.009")}{event.before_message_count}{typeof event.after_message_count==="number"?" → "+event.after_message_count:""}</p>}
    {typeof event.before_chars==="number"&&<p>{i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.010")}{event.before_chars}{typeof event.after_chars==="number"?" → "+event.after_chars:""}</p>}
    <p>{i18nCopy(zh, "copy.components_chat_ContextCompactionStatus.011")}</p>
  </div></details>;
}
