"use client";

/** Shared imperative dialogs. Radix owns modality, focus trapping and Escape. */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import * as Dialog from "@radix-ui/react-dialog";
import { useTranslations } from "next-intl";
import { CheckIcon, XIcon } from "../components/icons";

export type DialogTone = "default" | "danger" | "brand" | "warning";
export interface ConfirmOptions {
  title?: string;
  message: ReactNode;
  okLabel?: string;
  cancelLabel?: string;
  tone?: DialogTone;
}
export interface PromptOptions {
  title?: string;
  message: ReactNode;
  defaultValue?: string;
  placeholder?: string;
  okLabel?: string;
  cancelLabel?: string;
}
export interface AlertOptions {
  title?: string;
  message: ReactNode;
  okLabel?: string;
  tone?: DialogTone;
}
type DialogRequest =
  | { id: number; kind: "confirm"; options: ConfirmOptions; resolve: (value: boolean) => void }
  | { id: number; kind: "prompt"; options: PromptOptions; resolve: (value: string | null) => void }
  | { id: number; kind: "alert"; options: AlertOptions; resolve: () => void };
export type ToastTone = "default" | "ok" | "warn" | "error" | "brand";
export interface ToastOptions { message: ReactNode; tone?: ToastTone; durationMs?: number }
interface ToastEntry { id: number; message: ReactNode; tone: ToastTone; durationMs: number }
interface Dispatcher { enqueueDialog: (req: DialogRequest) => void; pushToast: (entry: ToastEntry) => void }
let dispatcher: Dispatcher | null = null;
let sequence = 0;
const nextId = () => ++sequence;
const stringifyMessage = (value: ReactNode) => typeof value === "string" || typeof value === "number" ? String(value) : "";

export function confirm(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (dispatcher) dispatcher.enqueueDialog({ id: nextId(), kind: "confirm", options, resolve });
    else resolve(typeof window !== "undefined" ? window.confirm(stringifyMessage(options.message)) : false);
  });
}
export function prompt(options: PromptOptions): Promise<string | null> {
  return new Promise((resolve) => {
    if (dispatcher) dispatcher.enqueueDialog({ id: nextId(), kind: "prompt", options, resolve });
    else resolve(typeof window !== "undefined" ? window.prompt(stringifyMessage(options.message), options.defaultValue ?? "") : null);
  });
}
export function alert(options: AlertOptions): Promise<void> {
  return new Promise((resolve) => {
    if (dispatcher) dispatcher.enqueueDialog({ id: nextId(), kind: "alert", options, resolve });
    else {
      if (typeof window !== "undefined") window.alert(stringifyMessage(options.message));
      resolve();
    }
  });
}
export function toast(options: ToastOptions): void {
  if (!dispatcher) return;
  const duration = options.durationMs ?? 4200;
  dispatcher.pushToast({ id: nextId(), message: options.message, tone: options.tone ?? "default", durationMs: Number.isFinite(duration) ? Math.max(0, duration) : 4200 });
}
function cancelRequest(req: DialogRequest) {
  if (req.kind === "confirm") req.resolve(false);
  else if (req.kind === "prompt") req.resolve(null);
  else req.resolve();
}

interface DialogContextValue { confirm: typeof confirm; prompt: typeof prompt; alert: typeof alert; toast: typeof toast }
const DialogContext = createContext<DialogContextValue | null>(null);
export function useDialogs(): DialogContextValue {
  return useContext(DialogContext) ?? { confirm, prompt, alert, toast };
}

export function DialogProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<DialogRequest[]>([]);
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const pending = useRef(new Map<number, DialogRequest>());
  const returnFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const owned: Dispatcher = {
      enqueueDialog(req) {
        if (!pending.current.size) returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        pending.current.set(req.id, req);
        setQueue((previous) => [...previous, req]);
      },
      pushToast(entry) {
        setToasts((previous) => {
          // Polling failures should not fill the viewport with identical notices.
          if (typeof entry.message === "string" && previous.some((item) => item.message === entry.message && item.tone === entry.tone)) return previous;
          return [...previous, entry].slice(-4);
        });
      },
    };
    dispatcher = owned;
    return () => {
      if (dispatcher === owned) dispatcher = null;
      for (const req of pending.current.values()) cancelRequest(req);
      pending.current.clear();
    };
  }, []);
  const onDone = useCallback((id: number) => {
    pending.current.delete(id);
    setQueue((previous) => previous.filter((req) => req.id !== id));
  }, []);
  const restoreFocus = useCallback(() => {
    if (!pending.current.size && returnFocus.current?.isConnected) returnFocus.current.focus();
  }, []);
  const dismissToast = useCallback((id: number) => setToasts((previous) => previous.filter((entry) => entry.id !== id)), []);
  const value = useMemo(() => ({ confirm, prompt, alert, toast }), []);
  const current = queue[0];
  return (
    <DialogContext.Provider value={value}>
      {children}
      {current ? <DialogShell key={current.id} req={current} onDone={onDone} restoreFocus={restoreFocus} /> : null}
      {toasts.length ? createPortal(
        <div className="ui-toast-stack">
          {toasts.map((entry) => <ToastItem key={entry.id} entry={entry} onDismiss={dismissToast} />)}
        </div>, document.body,
      ) : null}
    </DialogContext.Provider>
  );
}

function DialogShell({ req, onDone, restoreFocus }: { req: DialogRequest; onDone: (id: number) => void; restoreFocus: () => void }) {
  const t = useTranslations("ui");
  const inputRef = useRef<HTMLInputElement>(null);
  const okRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const resolved = useRef(false);
  const [draft, setDraft] = useState(req.kind === "prompt" ? req.options.defaultValue ?? "" : "");
  const tone = req.kind === "prompt" ? "default" : req.options.tone ?? "default";
  const title = req.options.title ?? t(req.kind === "prompt" ? "enterValue" : req.kind === "alert" ? "notice" : "confirm");
  const okLabel = req.options.okLabel ?? t(req.kind === "confirm" ? "confirm" : req.kind === "alert" ? "close" : "apply");
  function finish(accepted: boolean) {
    if (resolved.current) return;
    resolved.current = true;
    onDone(req.id);
    if (req.kind === "confirm") req.resolve(accepted);
    else if (req.kind === "prompt") req.resolve(accepted ? draft : null);
    else req.resolve();
  }
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) finish(false); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="ui-modal-overlay" />
        <Dialog.Content
          className="ui-dialog"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            if (req.kind === "prompt") { inputRef.current?.focus(); inputRef.current?.select(); }
            else if (req.kind === "confirm") cancelRef.current?.focus();
            else okRef.current?.focus();
          }}
          onCloseAutoFocus={(event) => { event.preventDefault(); restoreFocus(); }}
        >
          <Dialog.Title className="text-base font-semibold">{title}</Dialog.Title>
          <Dialog.Description asChild>
            <div className="mt-3 break-words text-sm leading-relaxed text-[color:var(--text-muted)]">{req.options.message}</div>
          </Dialog.Description>
          {req.kind === "prompt" ? (
            <input
              ref={inputRef}
              aria-label={title}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                  event.preventDefault(); finish(true);
                }
              }}
              placeholder={req.options.placeholder}
              autoComplete="off"
              className="input-dark mt-4 w-full"
            />
          ) : null}
          <div className="mt-6 flex flex-wrap justify-end gap-2">
            {req.kind !== "alert" ? <button ref={cancelRef} type="button" className="btn btn-ghost" onClick={() => finish(false)}>{req.options.cancelLabel ?? t("cancel")}</button> : null}
            <button ref={okRef} type="button" className={`btn ${tone === "danger" ? "ui-danger-button" : "btn-primary"}`} onClick={() => finish(true)}>
              <CheckIcon size={14} />{okLabel}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Each notice owns its timer; adding another never extends this one's lifetime. */
function ToastItem({ entry, onDismiss }: { entry: ToastEntry; onDismiss: (id: number) => void }) {
  const t = useTranslations("ui");
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const remaining = useRef(entry.durationMs);
  useEffect(() => {
    if (hovered || focused || entry.durationMs === 0) return;
    const started = Date.now();
    const timer = window.setTimeout(() => onDismiss(entry.id), remaining.current);
    return () => { window.clearTimeout(timer); remaining.current = Math.max(0, remaining.current - (Date.now() - started)); };
  }, [hovered, focused, entry.id, entry.durationMs, onDismiss]);
  return (
    <div
      role={entry.tone === "error" ? "alert" : "status"}
      className="ui-toast"
      data-tone={entry.tone}
      onPointerEnter={() => setHovered(true)}
      onPointerLeave={() => setHovered(false)}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false); }}
    >
      <div className="min-w-0 flex-1 break-words">{entry.message}</div>
      <button type="button" className="ui-icon-button shrink-0" aria-label={t("dismiss")} onClick={() => onDismiss(entry.id)}><XIcon size={15} /></button>
    </div>
  );
}
