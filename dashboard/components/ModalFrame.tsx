"use client";

import { useRef, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";

/** Shared modal boundary for existing editors and drawers. Content retains its
 * own heading/actions while Radix manages focus, modality and nested Escape. */
export function ModalFrame({ title, onClose, children, side = "center", width = "42rem", busy = false, testId, className = "" }: {
  title: string; onClose: () => void; children: ReactNode;
  side?: "center" | "right"; width?: string; busy?: boolean; testId?: string; className?: string;
}) {
  const returnFocus = useRef<HTMLElement | null>(null);
  return <Dialog.Root open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
    <Dialog.Portal>
      <Dialog.Overlay className="ui-panel-overlay" />
      <Dialog.Content className={`ui-panel ui-panel-${side} ${className}`} style={{ "--panel-width": width } as React.CSSProperties}
        aria-describedby={undefined} data-testid={testId}
        onOpenAutoFocus={() => { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
        onCloseAutoFocus={(event) => { if (returnFocus.current?.isConnected) { event.preventDefault(); returnFocus.current.focus({ preventScroll: true }); } }}
        onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
        onInteractOutside={(event) => { if (busy) event.preventDefault(); }}>
        <Dialog.Title className="sr-only">{title}</Dialog.Title>
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
