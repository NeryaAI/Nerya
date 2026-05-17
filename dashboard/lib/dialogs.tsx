"use client";

/**
 * Imperative dialog + toast API.
 *
 * Replaces ``window.confirm`` / ``window.alert`` / ``window.prompt`` —
 * each of which renders an OS-styled blocking popup that breaks the
 * airy aesthetic — with stylable React modals. The API is
 * imperative-flavoured so existing call sites can switch with a
 * one-line change:
 *
 *     - if (!window.confirm("delete?")) return;
 *     + if (!(await confirm({ message: "delete?" }))) return;
 *
 * Mount ``<DialogProvider>`` once near the root of the React tree
 * (``app/layout.tsx``). The imperative helpers fall back to native
 * dialogs if the provider hasn't mounted yet (e.g. SSR or unit tests),
 * so importing them is always safe.
 */

import {
  ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { CheckIcon, XIcon } from "../components/icons";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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
  | {
      id: number;
      kind: "confirm";
      options: ConfirmOptions;
      resolve: (value: boolean) => void;
    }
  | {
      id: number;
      kind: "prompt";
      options: PromptOptions;
      resolve: (value: string | null) => void;
    }
  | {
      id: number;
      kind: "alert";
      options: AlertOptions;
      resolve: () => void;
    };

export type ToastTone = "default" | "ok" | "warn" | "error" | "brand";

export interface ToastOptions {
  message: ReactNode;
  tone?: ToastTone;
  durationMs?: number;
}

interface ToastEntry {
  id: number;
  message: ReactNode;
  tone: ToastTone;
  durationMs: number;
}

// ---------------------------------------------------------------------------
// Imperative dispatcher (module-level singleton)
// ---------------------------------------------------------------------------

interface Dispatcher {
  enqueueDialog: (req: DialogRequest) => void;
  pushToast: (entry: ToastEntry) => void;
}

let _dispatcher: Dispatcher | null = null;
let _seq = 0;

function nextId() {
  _seq += 1;
  return _seq;
}

export function confirm(options: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    if (!_dispatcher) {
      if (typeof window !== "undefined") {
        const fallback = window.confirm(stringifyMessage(options.message));
        resolve(fallback);
      } else {
        resolve(false);
      }
      return;
    }
    _dispatcher.enqueueDialog({
      id: nextId(),
      kind: "confirm",
      options,
      resolve,
    });
  });
}

export function prompt(options: PromptOptions): Promise<string | null> {
  return new Promise((resolve) => {
    if (!_dispatcher) {
      if (typeof window !== "undefined") {
        const fallback = window.prompt(
          stringifyMessage(options.message),
          options.defaultValue ?? "",
        );
        resolve(fallback);
      } else {
        resolve(null);
      }
      return;
    }
    _dispatcher.enqueueDialog({
      id: nextId(),
      kind: "prompt",
      options,
      resolve,
    });
  });
}

export function alert(options: AlertOptions): Promise<void> {
  return new Promise((resolve) => {
    if (!_dispatcher) {
      if (typeof window !== "undefined") {
        window.alert(stringifyMessage(options.message));
      }
      resolve();
      return;
    }
    _dispatcher.enqueueDialog({
      id: nextId(),
      kind: "alert",
      options,
      resolve,
    });
  });
}

export function toast(options: ToastOptions): void {
  if (!_dispatcher) {
    if (typeof console !== "undefined") {
      console.info("[toast]", stringifyMessage(options.message));
    }
    return;
  }
  _dispatcher.pushToast({
    id: nextId(),
    message: options.message,
    tone: options.tone ?? "default",
    durationMs: options.durationMs ?? 4200,
  });
}

function stringifyMessage(value: ReactNode): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return "";
}

// ---------------------------------------------------------------------------
// Provider + Renderers
// ---------------------------------------------------------------------------

interface DialogContextValue {
  confirm: typeof confirm;
  prompt: typeof prompt;
  alert: typeof alert;
  toast: typeof toast;
}

const DialogContext = createContext<DialogContextValue | null>(null);

export function useDialogs(): DialogContextValue {
  const ctx = useContext(DialogContext);
  if (!ctx) {
    return { confirm, prompt, alert, toast };
  }
  return ctx;
}

export function DialogProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<DialogRequest[]>([]);
  const [toasts, setToasts] = useState<ToastEntry[]>([]);

  useEffect(() => {
    _dispatcher = {
      enqueueDialog: (req) => setQueue((prev) => [...prev, req]),
      pushToast: (entry) => setToasts((prev) => [...prev, entry]),
    };
    return () => {
      _dispatcher = null;
    };
  }, []);

  const popDialog = useCallback((id: number) => {
    setQueue((prev) => prev.filter((r) => r.id !== id));
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const value = useMemo<DialogContextValue>(
    () => ({ confirm, prompt, alert, toast }),
    [],
  );

  // Auto-dismiss toasts after their duration.
  useEffect(() => {
    if (!toasts.length) return;
    const timers = toasts.map((entry) =>
      setTimeout(() => dismissToast(entry.id), entry.durationMs),
    );
    return () => {
      timers.forEach((t) => clearTimeout(t));
    };
  }, [toasts, dismissToast]);

  return (
    <DialogContext.Provider value={value}>
      {children}
      <DialogQueue queue={queue} onDone={popDialog} />
      <ToastStack toasts={toasts} onDismiss={dismissToast} />
    </DialogContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// Modal rendering
// ---------------------------------------------------------------------------

function DialogQueue({
  queue,
  onDone,
}: {
  queue: DialogRequest[];
  onDone: (id: number) => void;
}) {
  // Only render the head of the queue so dialogs stack one at a time.
  // Subsequent requests appear after the current resolves.
  const current = queue[0];
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted || !current) return null;
  return createPortal(
    <DialogShell req={current} onDone={onDone} />,
    document.body,
  );
}

function DialogShell({
  req,
  onDone,
}: {
  req: DialogRequest;
  onDone: (id: number) => void;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const okRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [draft, setDraft] = useState<string>(
    req.kind === "prompt" ? req.options.defaultValue ?? "" : "",
  );

  function close(result: boolean | string | null | void) {
    onDone(req.id);
    // ts can't narrow result against req.kind, so we dispatch by kind.
    if (req.kind === "confirm") req.resolve(Boolean(result));
    else if (req.kind === "prompt")
      req.resolve(result === false ? null : (result as string | null));
    else req.resolve();
  }

  useEffect(() => {
    if (req.kind === "prompt") {
      inputRef.current?.focus();
      inputRef.current?.select?.();
    } else {
      okRef.current?.focus();
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        if (req.kind === "alert") close();
        else close(false);
      } else if (event.key === "Enter" && req.kind !== "prompt") {
        close(req.kind === "alert" ? undefined : true);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [req.id]);

  const tone =
    req.kind === "alert"
      ? req.options.tone ?? "default"
      : req.kind === "confirm"
      ? req.options.tone ?? "default"
      : "default";

  const okClass =
    tone === "danger"
      ? "bg-rose-500 text-white hover:bg-rose-400"
      : tone === "warning"
      ? "bg-amber-500 text-white hover:bg-amber-400"
      : tone === "brand"
      ? "bg-brand-500 text-white hover:bg-brand-400"
      : "bg-brand-500 text-white hover:bg-brand-400";

  const okLabel =
    req.kind === "confirm"
      ? req.options.okLabel ?? "Confirm"
      : req.kind === "prompt"
      ? req.options.okLabel ?? "OK"
      : req.options.okLabel ?? "OK";

  const cancelLabel =
    req.kind === "confirm"
      ? req.options.cancelLabel ?? "Cancel"
      : req.kind === "prompt"
      ? req.options.cancelLabel ?? "Cancel"
      : "";

  const title =
    req.options.title ??
    (req.kind === "confirm"
      ? "Confirm"
      : req.kind === "prompt"
      ? "Enter a value"
      : "Notice");

  const message = req.options.message;

  const accent =
    tone === "danger"
      ? "bg-rose-500"
      : tone === "warning"
      ? "bg-amber-400"
      : tone === "brand"
      ? "bg-brand-500"
      : "bg-brand-500";

  return (
    <div
      role="presentation"
      className="fixed inset-0 z-[1200] flex items-center justify-center px-4"
    >
      <button
        type="button"
        aria-label="Close"
        onClick={() => (req.kind === "alert" ? close() : close(false))}
        className="absolute inset-0 bg-black/55 backdrop-blur-[3px] cursor-default"
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        className="relative rounded-2xl border border-brand-500/15 bg-[#0c0a18]/95 backdrop-blur-airy shadow-airy overflow-hidden"
        style={{ width: "min(90vw, 500px)" }}
      >
        <span className={`absolute inset-x-0 top-0 h-px ${accent} opacity-60`} />
        <header className="flex items-start gap-3 px-5 pt-5 pb-2">
          <div className="flex-1 min-w-0">
            <h2 className="text-[15px] font-semibold tracking-tight text-white">
              {title}
            </h2>
          </div>
        </header>
        <div className="px-5 pb-4 text-[13px] text-ink-200 leading-relaxed break-words">
          {message}
          {req.kind === "prompt" ? (
            <input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  close(draft);
                }
              }}
              placeholder={req.options.placeholder ?? ""}
              spellCheck={false}
              autoComplete="off"
              className="mt-3 w-full rounded-lg border border-brand-500/15 bg-ink-900/80 px-3 py-2 text-[13px] text-ink-100 font-mono focus:outline-none focus:border-brand-500/50"
            />
          ) : null}
        </div>
        <footer className="flex items-center justify-end gap-2 px-5 pb-4 pt-1 border-t border-brand-500/10 bg-white/[0.015]">
          {req.kind !== "alert" ? (
            <button
              type="button"
              onClick={() => close(false)}
              className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full border border-brand-500/15 text-ink-300 text-[12px] font-semibold transition-colors hover:bg-white/[0.04] hover:text-white cursor-pointer"
            >
              <XIcon size={12} />
              {cancelLabel}
            </button>
          ) : null}
          <button
            ref={okRef}
            type="button"
            onClick={() => {
              if (req.kind === "prompt") close(draft);
              else if (req.kind === "alert") close();
              else close(true);
            }}
            className={`inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full text-[12px] font-semibold transition-colors cursor-pointer ${okClass}`}
          >
            <CheckIcon size={12} />
            {okLabel}
          </button>
        </footer>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toast stack
// ---------------------------------------------------------------------------

const TOAST_TONE_STYLES: Record<ToastTone, string> = {
  default: "border-brand-500/15 bg-[#0c0a18]/90 text-ink-100",
  ok: "border-emerald-400/30 bg-emerald-500/[0.10] text-emerald-100",
  warn: "border-amber-400/30 bg-amber-500/[0.10] text-amber-100",
  error: "border-rose-400/30 bg-rose-500/[0.10] text-rose-100",
  brand: "border-brand-500/30 bg-brand-500/[0.12] text-brand-100",
};

function ToastStack({
  toasts,
  onDismiss,
}: {
  toasts: ToastEntry[];
  onDismiss: (id: number) => void;
}) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted || !toasts.length) return null;
  return createPortal(
    <div className="fixed bottom-4 right-4 z-[1300] flex flex-col gap-2 items-end pointer-events-none">
      {toasts.map((entry) => (
        <div
          key={entry.id}
          role="status"
          className={`pointer-events-auto flex items-start gap-2 max-w-[360px] rounded-xl border backdrop-blur-airy shadow-airy px-3.5 py-2.5 text-[12.5px] leading-snug ${TOAST_TONE_STYLES[entry.tone]}`}
        >
          <div className="flex-1 min-w-0 break-words">{entry.message}</div>
          <button
            type="button"
            onClick={() => onDismiss(entry.id)}
            className="opacity-50 hover:opacity-100 transition-opacity cursor-pointer p-0.5 rounded-md"
            aria-label="Dismiss"
          >
            <XIcon size={11} />
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}
