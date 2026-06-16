"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import type { NativeBlock } from "../../../lib/chat";
import { CopyButton, Tag, ToolRowCard } from "./atoms";
import { recordOf } from "./helpers";

function skillBodyFromBlock(block: NativeBlock): {
  name: string;
  args: string;
  baseDir: string;
  body: string;
  raw: string;
} {
  const payload = recordOf(block.payload);
  const result = recordOf(block.result);
  const name = String(payload.skill || result.skill || block.action || "");
  const args = String(payload.args || "");

  // ``Skill`` tool result text starts with:
  //   ``Base directory for this skill: <abs path>\n\n<markdown body>``
  // Pull both pieces out so we can render them as separate blocks.
  let raw = "";
  if (typeof block.result === "string") {
    raw = block.result;
  } else if (Array.isArray(result.content)) {
    raw = result.content
      .map((part) => {
        const r = recordOf(part);
        if (typeof r.text === "string") return r.text;
        return "";
      })
      .filter(Boolean)
      .join("\n");
  } else if (typeof result.text === "string") {
    raw = result.text;
  }
  let baseDir = "";
  let body = raw.trim();
  const m = body.match(/^Base directory for this skill:\s*(.+?)\s*\n([\s\S]*)$/);
  if (m) {
    baseDir = m[1].trim();
    body = m[2].trim();
  }
  return { name, args, baseDir, body, raw };
}

export function SkillLoadCard({
  block,
  variant,
  pending = false,
}: {
  block: NativeBlock;
  variant: "use" | "result";
  pending?: boolean;
}) {
  const t = useTranslations("skillLoadCard");
  const [expanded, setExpanded] = useState(false);
  const { name, args, baseDir, body, raw } = skillBodyFromBlock(block);
  const ok = block.ok !== false && !block.error;
  const isResult = variant === "result";
  const preview = body.split("\n").slice(0, 6).join("\n");
  const hasMore = body.length > preview.length;
  return (
    <ToolRowCard
      icon={
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2l2.5 4.9L20 8l-4 3.8.9 5.4L12 14.8 7.1 17.2 8 11.8 4 8l5.5-1.1L12 2z" />
        </svg>
      }
      title={
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <span>{isResult ? t("loaded") : t("loading")}</span>
          {pending ? (
            <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
              <span className="typing-dot" />
            </span>
          ) : null}
        </span>
      }
      subtitle={
        <span>
          {name || t("unnamed")}
          {args ? ` · ${args}` : ""}
        </span>
      }
      tone={ok ? "brand" : "err"}
      defaultOpen={pending}
      meta={
        <>
          {isResult ? (
            ok ? (
              <Tag tone="ok">{t("ready")}</Tag>
            ) : (
              <Tag tone="err">{(block.error_kind as string | undefined) || "error"}</Tag>
            )
          ) : (
            <Tag tone="brand">SKILL</Tag>
          )}
          {typeof block.elapsed_ms === "number" ? <Tag>{block.elapsed_ms}ms</Tag> : null}
        </>
      }
    >

      {args ? (
        <div className="rounded-lg border border-brand-500/15 bg-brand-500/[0.04] px-3 py-1.5">
          <div className="text-[11px] text-ink-500 font-medium mb-0.5">
            {t("arguments")}
          </div>
          <div className="text-[12px] text-ink-200 font-mono break-words">{args}</div>
        </div>
      ) : null}

      {baseDir ? (
        <div className="flex items-center gap-2 flex-wrap">
          <Tag tone="brand">{t("baseDir")}</Tag>
          <span className="font-mono text-[11px] text-ink-300 break-all">{baseDir}</span>
        </div>
      ) : null}

      {isResult && body ? (
        <div className="rounded-xl border border-brand-500/15 bg-ink-900/40 px-3 py-2.5">
          <div className="text-[11px] text-ink-500 font-medium mb-1.5">
            {t("playbook")}
          </div>
          <div
            className={`text-[12px] leading-relaxed text-ink-200 whitespace-pre-wrap font-sans transition-[max-height] ${
              expanded ? "max-h-[640px] overflow-auto" : "max-h-32 overflow-hidden"
            }`}
          >
            {expanded ? body : preview}
          </div>
          {hasMore ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-2 text-[12px] text-brand-300 hover:text-brand-200 cursor-pointer transition-colors"
            >
              {expanded ? t("collapsePlaybook") : t("expandPlaybook")}
            </button>
          ) : null}
          {expanded && raw ? (
            <div className="mt-2 flex justify-end">
              <CopyButton text={body} />
            </div>
          ) : null}
        </div>
      ) : null}

      {isResult && block.error ? (
        <div className="text-[12px] text-danger">{String(block.error)}</div>
      ) : null}
    </ToolRowCard>
  );
}
