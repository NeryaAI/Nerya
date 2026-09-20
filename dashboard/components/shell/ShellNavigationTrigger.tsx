"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useTranslations } from "next-intl";
import { PanelLeftIcon } from "../icons";

/** Uses the AppShell drawer so every surface shares its focus and Escape behavior. */
export function ShellNavigationTrigger() {
  const t = useTranslations("ui");
  return <div className="shrink-0 md:hidden">
    <Dialog.Trigger asChild>
      <button type="button" className="ui-icon-button h-10 w-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400" aria-label={t("openNavigation")}>
        <PanelLeftIcon size={20} />
      </button>
    </Dialog.Trigger>
  </div>;
}
