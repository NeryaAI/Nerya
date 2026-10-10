"use client";

import { useRef, useState, type ReactNode, type PointerEvent } from "react";
import { useTranslations } from "next-intl";

export function ModelRoutePriority({ group, index, count, disabled, onMove, children, className = "" }: {
  group: string;
  index: number;
  count: number;
  disabled?: boolean;
  onMove: (from: number, to: number) => void;
  children: ReactNode;
  className?: string;
}) {
  const t = useTranslations("settings.modelCard");
  const [dragging, setDragging] = useState(false);
  const [targetIndex, setTargetIndex] = useState<number | null>(null);
  const active = useRef(false);
  const target = useRef<HTMLElement | null>(null);
  const locked = disabled || count < 2;
  function clearTarget() {
    target.current?.removeAttribute("data-priority-drop");
    target.current = null;
    setTargetIndex(null);
  }
  function findTarget(event: PointerEvent<HTMLButtonElement>) {
    const row = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>("[data-model-priority-group]");
    return row?.dataset.modelPriorityGroup === group && !row.dataset.priorityDisabled ? row : null;
  }
  return <div data-model-priority-group={group} data-model-priority-index={index} data-priority-disabled={locked || undefined}
    className={`rounded-lg border border-[color:var(--line)] p-3 data-[priority-drop=true]:border-[color:var(--brand)] ${dragging ? "opacity-60" : ""} ${className}`}>
    <div className="mb-2 flex items-center gap-2">
      <button type="button" disabled={locked} className="icon-btn h-7 w-7 shrink-0 cursor-grab touch-none active:cursor-grabbing"
        aria-label={t("dragPriority", { index: index + 1 })} title={t("dragPriority", { index: index + 1 })}
        onKeyDown={(event) => {
          if (event.key === "Escape") { active.current = false; clearTarget(); setDragging(false); }
          const next = event.key === "ArrowUp" ? index - 1 : event.key === "ArrowDown" ? index + 1 : index;
          if (next !== index && next >= 0 && next < count && !locked) { event.preventDefault(); onMove(index, next); }
        }}
        onPointerDown={(event) => {
          if (locked || event.button !== 0) return;
          event.currentTarget.setPointerCapture(event.pointerId);
          active.current = true;
          setDragging(true);
        }}
        onPointerMove={(event) => {
          if (!active.current) return;
          const row = findTarget(event);
          if (row === target.current) return;
          clearTarget();
          if (row) { target.current = row; row.dataset.priorityDrop = "true"; setTargetIndex(Number(row.dataset.modelPriorityIndex)); }
        }}
        onPointerUp={(event) => {
          const row = active.current ? findTarget(event) : null;
          active.current = false;
          clearTarget(); setDragging(false);
          if (row && !locked) onMove(index, Number(row.dataset.modelPriorityIndex));
        }}
        onPointerCancel={() => { active.current = false; clearTarget(); setDragging(false); }}
        onLostPointerCapture={() => { active.current = false; clearTarget(); setDragging(false); }}>
        <svg width="14" height="16" viewBox="0 0 14 16" fill="currentColor" aria-hidden="true">
          {[3, 8, 13].flatMap(y => [4, 10].map(x => <circle key={`${x}-${y}`} cx={x} cy={y} r="1.3" />))}
        </svg>
      </button>
      <span className="text-xs text-[color:var(--text-muted)]">{t("priorityLabel", { index: index + 1 })}</span>
      <span className="sr-only" aria-live="polite">{targetIndex !== null ? t("priorityLabel", { index: targetIndex + 1 }) : ""}</span>
      <div className="ml-auto flex gap-1">
        <button type="button" className="icon-btn h-7 w-7" disabled={locked || index === 0} onClick={() => onMove(index, index - 1)} aria-label={t("movePriorityUp", { index: index + 1 })}>↑</button>
        <button type="button" className="icon-btn h-7 w-7" disabled={locked || index === count - 1} onClick={() => onMove(index, index + 1)} aria-label={t("movePriorityDown", { index: index + 1 })}>↓</button>
      </div>
    </div>
    {children}
  </div>;
}
