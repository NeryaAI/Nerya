"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect } from "react";
import { useLocale } from "next-intl";
import { confirm } from "../../lib/dialogs";

/** In-app links warn; programmatic navigation/Back is protected by draft storage. */
export function useUnsavedChanges(dirty: boolean) {
  const zh = useLocale().startsWith("zh");
  useEffect(() => {
    if (!dirty) return;
    let asking = false, disposed = false;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    const click = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href]") : null;
      if (!anchor || anchor.target === "_blank" || anchor.download || anchor.href === location.href || anchor.getAttribute("href")?.startsWith("#")) return;
      event.preventDefault(); event.stopPropagation();
      if (asking) return;
      asking = true;
      void confirm({ title: i18nCopy(zh, "copy.components_chat_useUnsavedChanges.001"),
        message: i18nCopy(zh, "copy.components_chat_useUnsavedChanges.002"),
        okLabel: i18nCopy(zh, "copy.components_chat_useUnsavedChanges.003"), cancelLabel: i18nCopy(zh, "copy.components_chat_useUnsavedChanges.004") }).then(ok => {
          asking = false;
          if (ok && !disposed) { window.removeEventListener("beforeunload",beforeUnload); window.location.assign(anchor.href); }
        });
    };
    window.addEventListener("beforeunload",beforeUnload); document.addEventListener("click",click,true);
    return () => { disposed = true; window.removeEventListener("beforeunload",beforeUnload); document.removeEventListener("click",click,true); };
  }, [dirty,zh]);
}
