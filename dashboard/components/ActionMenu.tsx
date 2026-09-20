"use client";

import { useRef, type ReactNode } from "react";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { useLocale } from "next-intl";
import { ChevronDownIcon } from "./icons";
import { toast } from "../lib/dialogs";

export type MenuAction = { key: string; label: ReactNode; onSelect: () => void | Promise<unknown>; disabled?: boolean; danger?: boolean };

/** Always-visible row actions, usable by touch and keyboard as well as mouse. */
export function ActionMenu({ label, items, disabled }: { label: string; items: (MenuAction | false | null)[]; disabled?: boolean }) {
  const zh = useLocale().startsWith("zh");
  const pending = useRef<MenuAction["onSelect"] | null>(null);
  const actions = items.filter((item): item is MenuAction => Boolean(item));
  return <Menu.Root>
    <Menu.Trigger asChild><button type="button" className="btn btn-ghost" aria-label={label} disabled={disabled || !actions.length}>
      {zh ? "操作" : "Actions"}<ChevronDownIcon size={13} />
    </button></Menu.Trigger>
    <Menu.Portal><Menu.Content className="ui-select-menu" align="end" sideOffset={6} collisionPadding={8}
      onCloseAutoFocus={() => {
        const action = pending.current;
        pending.current = null;
        // Let Radix restore the trigger before a follow-up confirmation opens.
        if (action) queueMicrotask(() => { Promise.resolve().then(action).catch((error) => toast({ message: String(error instanceof Error ? error.message : error), tone: "error" })); });
      }}>
      {actions.map((item) => <Menu.Item key={item.key} className={`ui-select-option ${item.danger ? "text-danger" : ""}`}
        disabled={item.disabled} onSelect={() => { pending.current = item.onSelect; }}>{item.label}</Menu.Item>)}
    </Menu.Content></Menu.Portal>
  </Menu.Root>;
}
