"use client";

/**
 * `<TermTip>` — inline plain-language explainer for operator-facing jargon.
 *
 * Renders a dashed-underline term (or a small "?" badge when no children
 * are given). Activating it opens a compact portal popover with a
 * plain-language explanation from the `terms` i18n namespace, so a term
 * like "vault ref" or "model tier" never has to be a dead end.
 *
 * Behaviour:
 *  • Click / keyboard activation (Enter, Space) toggles the popover.
 *  • Escape and outside click close.
 *  • The panel renders through a portal to `document.body` with
 *    `position: fixed`, mirroring `PortalDropdown`, so scroll containers
 *    (settings lists, gateway forms) can never clip it.
 *
 * Usage:
 *   <TermTip term="vaultRef" />                        // "?" badge
 *   <TermTip term="tier">model tiers</TermTip>         // explains the phrase
 */

import { ReactNode, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";

const POPOVER_WIDTH = 300;
const VIEWPORT_PADDING = 8;

export function TermTip({
  term,
  children,
  className,
}: {
  term: string;
  children?: ReactNode;
  className?: string;
}) {
  const t = useTranslations("terms");
  const popoverId = useId();
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [style, setStyle] = useState<{
    top: number;
    left: number;
    maxHeight: number;
  } | null>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!open) return;

    function place() {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      // Open below the trigger; flip above when the viewport bottom is
      // closer than a typical explanation needs.
      const estimatedHeight = 150;
      let top = rect.bottom + 6;
      if (top + estimatedHeight > window.innerHeight - VIEWPORT_PADDING) {
        top = Math.max(VIEWPORT_PADDING, rect.top - estimatedHeight - 6);
      }
      const left = Math.min(
        Math.max(VIEWPORT_PADDING, rect.left),
        window.innerWidth - POPOVER_WIDTH - VIEWPORT_PADDING,
      );
      setStyle({
        top,
        left,
        maxHeight: Math.min(260, window.innerHeight - top - VIEWPORT_PADDING),
      });
    }

    place();
    const onScrollOrResize = () => place();
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (
        panelRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const label = t(`${term}.label`);
  const explain = t(`${term}.explain`);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`term-tip ${className ?? ""}`}
        aria-expanded={open}
        aria-controls={popoverId}
        aria-label={children ? undefined : label}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        {children ?? (
          <span
            className="term-tip-badge"
            aria-hidden
          >
            ?
          </span>
        )}
      </button>
      {mounted && open && style
        ? createPortal(
            <div
              ref={panelRef}
              id={popoverId}
              role="tooltip"
              className="term-tip-popover"
              style={{ ...style, width: POPOVER_WIDTH }}
              tabIndex={-1}
            >
              <div className="term-tip-title">{label}</div>
              <p className="term-tip-body">{explain}</p>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
