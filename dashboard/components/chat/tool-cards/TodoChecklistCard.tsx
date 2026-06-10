"use client";

import type { NativeBlock } from "../../../lib/chat";
import { Tag } from "./atoms";
import { recordOf } from "./helpers";

export type TodoStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "cancelled"
  | string;

export interface TodoItemShape {
  id?: string;
  content?: string;
  activeForm?: string;
  active_form?: string;
  status?: TodoStatus;
}

/**
 * ``todo_write`` shape — both ``payload.todos`` (tool_use) and
 * ``result.todos`` / ``result.content[1].input.todos`` (tool_result)
 * can carry the list. We try them in order so the renderer always
 * grabs the freshest representation regardless of which envelope side
 * it sees.
 */
export function todosFromBlock(block: NativeBlock): TodoItemShape[] {
  const payload = recordOf(block.payload);
  const result = recordOf(block.result);
  const candidates: unknown[] = [
    payload.todos,
    result.todos,
    (result.content as unknown[] | undefined)?.flatMap?.((c) => {
      const r = recordOf(c);
      const inner = recordOf(r.input);
      return inner.todos ?? [];
    }) ?? null,
  ];
  for (const c of candidates) {
    if (Array.isArray(c) && c.length) {
      return c.filter(
        (row): row is TodoItemShape => !!row && typeof row === "object",
      );
    }
  }
  return [];
}

function todoStatusMeta(status: TodoStatus): {
  label: string;
  tone: "neutral" | "ok" | "warn" | "err" | "brand";
  glyph: string;
  ring: string;
  fill: string;
} {
  switch (status) {
    case "completed":
      return {
        label: "done",
        tone: "ok",
        glyph: "\u2713",
        ring: "border-emerald-400/50",
        fill: "bg-emerald-400/15 text-emerald-300",
      };
    case "in_progress":
      return {
        label: "in progress",
        tone: "brand",
        glyph: "\u25B6",
        ring: "border-brand-400/60",
        fill: "bg-brand-400/15 text-brand-200",
      };
    case "cancelled":
      return {
        label: "cancelled",
        tone: "warn",
        glyph: "/",
        ring: "border-ink-500/60",
        fill: "bg-ink-700/40 text-ink-400 line-through",
      };
    case "pending":
    default:
      return {
        label: "pending",
        tone: "neutral",
        glyph: "",
        ring: "border-brand-500/25",
        fill: "bg-brand-500/[0.05] text-ink-200",
      };
  }
}

export function TodoChecklistCard({
  todos,
  pending = false,
}: {
  todos: TodoItemShape[];
  pending?: boolean;
}) {
  const total = todos.length;
  const completed = todos.filter((t) => t.status === "completed").length;
  const inProgress = todos.find((t) => t.status === "in_progress");
  const progress = total > 0 ? Math.round((completed / total) * 100) : 0;
  return (
    <div className="rounded-md border border-brand-500/15 bg-brand-500/[0.05] px-4 py-3.5 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-flex items-center justify-center w-6 h-6 rounded-lg bg-brand-500/15 border border-brand-500/30 text-brand-200">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 11l3 3L22 4" />
              <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
            </svg>
          </span>
          <span className="text-[13px] font-semibold text-ink-100 tracking-tight">
            Todo list
          </span>
          {pending ? (
            <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
              <span className="typing-dot" />
              <span>updating</span>
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <Tag tone="brand">{`${completed}/${total} done`}</Tag>
          {inProgress?.activeForm || inProgress?.active_form ? (
            <Tag tone="brand">
              {String(inProgress.activeForm || inProgress.active_form).slice(0, 32)}
            </Tag>
          ) : null}
        </div>
      </div>
      {total > 0 ? (
        <div className="h-1.5 rounded-full bg-ink-900/70 border border-brand-500/10 overflow-hidden">
          <div
            className="h-full rounded-full bg-brand-300/80 transition-all"
            style={{ width: `${progress}%` }}
          />
        </div>
      ) : null}
      <ul className="space-y-1.5">
        {todos.length === 0 ? (
          <li className="text-[12px] text-ink-400 italic">(no todos yet)</li>
        ) : null}
        {todos.map((todo, i) => {
          const meta = todoStatusMeta(String(todo.status || "pending"));
          return (
            <li
              key={String(todo.id || i)}
              className={`flex items-start gap-2.5 rounded-xl px-2.5 py-2 transition-colors ${
                todo.status === "completed"
                  ? "bg-emerald-400/[0.04]"
                  : todo.status === "in_progress"
                  ? "bg-brand-400/[0.06]"
                  : ""
              }`}
            >
              <span
                className={`mt-[2px] inline-flex items-center justify-center w-4 h-4 shrink-0 rounded-md border text-[10px] font-medium leading-none ${meta.ring} ${meta.fill}`}
                aria-hidden
              >
                {meta.glyph}
              </span>
              <div className="flex-1 min-w-0">
                <div
                  className={`text-[12.5px] leading-snug ${
                    todo.status === "completed"
                      ? "text-ink-400 line-through"
                      : todo.status === "cancelled"
                      ? "text-ink-500 line-through"
                      : "text-ink-100"
                  }`}
                >
                  {String(todo.content || todo.activeForm || todo.active_form || "")}
                </div>
                {todo.status === "in_progress" && (todo.activeForm || todo.active_form) ? (
                  <div className="mt-0.5 text-[10.5px] text-brand-200/80">
                    {String(todo.activeForm || todo.active_form)}
                  </div>
                ) : null}
              </div>
              <Tag tone={meta.tone}>{meta.label}</Tag>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
