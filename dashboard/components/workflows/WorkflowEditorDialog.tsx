"use client";

import { useRef, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import ui from "./WorkflowNative.module.css";

/** One task-focused overlay, using the application's existing modal layer.
 * Closing this editor never discards the parent's staged changes. */
export function WorkflowEditorDialog({ open, title, children, footer, onClose, focusAfterClose }: {
  open: boolean; title: string; children: ReactNode; footer?: ReactNode;
  onClose: () => void; focusAfterClose?: () => HTMLElement | null | undefined;
}) {
  const returnFocus = useRef<HTMLElement | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  return <Dialog.Root open={open} onOpenChange={(value) => { if (!value) onClose(); }}>
    <Dialog.Portal><Dialog.Overlay className="ui-modal-overlay" />
      <Dialog.Content className={`ui-dialog ${ui.editorDialog}`} data-testid="workflow-editor-dialog" aria-describedby={undefined}
        onOpenAutoFocus={(event) => { event.preventDefault(); returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; heading.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); const target = focusAfterClose?.() || returnFocus.current; if (target?.isConnected) target.focus({ preventScroll: true }); }}>
        <Dialog.Title ref={heading} tabIndex={-1} className="sr-only">{title}</Dialog.Title>
        {children}
        {footer && <footer className={ui.editorFooter}>{footer}</footer>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
