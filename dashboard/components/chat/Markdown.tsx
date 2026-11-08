"use client";

import {
  cloneElement,
  isValidElement,
  memo,
  type MouseEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { Components } from "react-markdown";
import { useTranslations } from "next-intl";
import { CopyIcon } from "../icons";
import { toast } from "../../lib/dialogs";

/** Relative document/code/data references retain a raw URL as a navigation fallback. */
function workspaceArtifactHref(href: string | undefined): string | undefined {
  if (!href || /^(?:[a-z][a-z0-9+.-]*:|\/|#|\?)/i.test(href)) return href;
  let path: string;
  try { path = decodeURIComponent(href).replace(/^\.\//, ""); } catch { return href; }
  // Do not reinterpret application links, encoded indirection or traversal.
  if (/[\\%:?#\x00-\x1f\x7f]/.test(path) || path.replace(/\/$/, "").split("/").some(part => !part || part === "." || part === "..")) return href;
  const candidate = /^evolution\/proposals\/([^/]+)\/after\/strategies\/([^/]+)\/?$/.exec(path);
  if (candidate) return "/strategies?strategy_id=" + encodeURIComponent(candidate[2]) + "&proposal_id=" + encodeURIComponent(candidate[1]);
  const installed = /^strategies\/([^/]+)\/?$/.exec(path);
  if (installed) return "/strategies?strategy_id=" + encodeURIComponent(installed[1]);
  if (!/\.(?:md|txt|json|csv|tsv|ya?ml|toml|py|js|ts|tsx|jsx|sql|ipynb|pdf|png|jpe?g|webp|svg|xlsx?|docx?|pptx?)$/i.test(path)) return href;
  return "/api/proxy/workspace/file/raw/" + path.split("/").map(encodeURIComponent).join("/");
}

/** Route ordinary Markdown file clicks through the workspace's existing opener. */
export function openWorkspaceFileLink(event: MouseEvent<HTMLElement>, onOpenFile?: (path: string) => void) {
  if (!onOpenFile || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const anchor = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>(".nerya-markdown a[href]") : null;
  if (!anchor || anchor.hasAttribute("download")) return;
  const prefix = "/api/proxy/workspace/file/raw/";
  const href = anchor.getAttribute("href") || "";
  const resource = workspaceArtifactHref(href.startsWith(prefix) ? href.slice(prefix.length) : href);
  if (!resource?.startsWith(prefix)) return;
  event.preventDefault();
  onOpenFile(decodeURIComponent(resource.slice(prefix.length)));
}

/**
 * Shared markdown renderer for the chat surface.
 *
 * The Apr-27 user feedback called for proper markdown rendering in the
 * chat bubble (and the gateways) — the runtime already does this, and the
 * agent's prose summaries routinely contain headings, code blocks,
 * tables, and bullet lists that look terrible as ``whitespace-pre-wrap``
 * plain text.
 *
 * Design notes:
 *
 * - We use ``react-markdown`` + ``remark-gfm`` so GitHub-flavoured
 *   markdown (tables, task lists, autolinks, strikethrough) renders
 *   correctly.
 * - ``rehype-highlight`` adds syntax highlighting for fenced code
 *   blocks; we ship the highlight stylesheet from
 *   ``app/globals.css`` so themes line up with the rest of the chat.
 * - All custom renderers are styled with the existing Tailwind ink/brand
 *   palette so a markdown-rendered bubble feels native to the dashboard
 *   instead of a Stack Overflow drop-in.
 */

const components: Components = {
  h1: ({ children }) => (
    <h1 className="text-base font-semibold text-ink-50 mb-2 mt-3 first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="text-sm font-semibold text-ink-50 mb-1.5 mt-3 first:mt-0">
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="text-sm font-semibold text-ink-100 mb-1.5 mt-2 first:mt-0">
      {children}
    </h3>
  ),
  p: ({ children }) => (
    <p className="text-sm text-ink-100 leading-relaxed mb-2 last:mb-0">
      {children}
    </p>
  ),
  ul: ({ children }) => (
    <ul className="list-disc pl-5 space-y-1 mb-2 last:mb-0 text-sm text-ink-100">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal pl-5 space-y-1 mb-2 last:mb-0 text-sm text-ink-100">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={workspaceArtifactHref(href)}
      target="_blank"
      rel="noreferrer noopener"
      className="text-brand-300 hover:text-brand-200 underline break-words"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="rounded-md border border-brand-500/20 bg-brand-500/5 px-3 py-2 italic text-ink-200 my-2">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="border-ink-700/60 my-2" />,
  table: ({ children }) => (
    <div className="overflow-x-auto my-2">
      <table className="text-xs border border-ink-700/60 rounded-md">
        {children}
      </table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="bg-ink-800/50 text-ink-200">{children}</thead>
  ),
  th: ({ children }) => (
    <th className="min-w-[6em] px-2 py-1 text-left border-b border-ink-700/60 font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="min-w-[6em] px-2 py-1 border-b border-ink-700/40 text-ink-100">
      {children}
    </td>
  ),
  // react-markdown v10 removed the legacy ``props.inline`` flag, so the
  // old inline branch here was dead and inline snippets rendered
  // unstyled. Inline ``code`` never carries a ``language-*`` class;
  // block code is always wrapped by ``pre`` (CodeBlock below), which
  // re-styles its nested ``<code>`` element — so this renderer can
  // safely apply the inline chip style unconditionally.
  code({ className, children, node, ...rest }) {
    void node;
    return (
      <code
        className={`px-1 py-0.5 rounded bg-ink-800/70 text-brand-200 font-mono text-[12px] ${className ?? ""}`}
        {...rest}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  strong: ({ children }) => (
    <strong className="font-semibold text-ink-50">{children}</strong>
  ),
  em: ({ children }) => <em className="italic text-ink-100">{children}</em>,
};

export function hasUnclosedFence(value: string): boolean {
  let fences = 0;
  for (const line of value.split(/\r?\n/)) {
    if (/^\s*(`{3,}|~{3,})/.test(line)) fences += 1;
  }
  return fences % 2 === 1;
}

/** Flatten a rendered code element back to raw text (for copy-to-clipboard). */
function extractText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractText(node.props.children);
  }
  return "";
}

/**
 * Block-code shell — keeps the original ``pre`` styling and adds a slim
 * header with the fence language plus a copy button (feedback via the
 * shared toast).
 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const tChat = useTranslations("chat");
  const tCommon = useTranslations("common");
  const child = (Array.isArray(children) ? children[0] : children) as
    | ReactElement<{ className?: string; children?: ReactNode }>
    | undefined;
  const fenceClass =
    child && isValidElement(child)
      ? String(child.props.className ?? "")
      : "";
  const language = /language-([\w+#.-]+)/.exec(fenceClass)?.[1] ?? "";
  const raw = child && isValidElement(child)
    ? extractText(child.props.children).replace(/\n$/, "")
    : "";

  async function copyCode() {
    if (!raw) return;
    try {
      await navigator.clipboard.writeText(raw);
      toast({ tone: "ok", message: tChat("copied") });
    } catch {
      // Clipboard unavailable (insecure context / denied) — the visitor
      // can still select the block manually.
    }
  }

  // The ``code`` renderer above applies the inline chip style to every
  // ``<code>``; inside a block we swap it for the plain block styling
  // (syntax-highlight spans inside are untouched).
  const code = isValidElement(child)
    ? cloneElement(child, { className: "font-mono text-[12px]" })
    : children;

  return (
    <div className="my-2 overflow-hidden rounded-md border border-ink-700/60 bg-ink-900/70">
      <div className="flex items-center justify-between border-b border-ink-700/40 px-3 py-1">
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-400">
          {language || "code"}
        </span>
        <button
          type="button"
          onClick={() => void copyCode()}
          aria-label={tCommon("copy")}
          title={tCommon("copy")}
          className="inline-flex cursor-pointer items-center rounded p-1 text-ink-400 transition-colors hover:bg-white/5 hover:text-ink-100"
        >
          <CopyIcon size={12} />
        </button>
      </div>
      <pre className="overflow-x-auto p-3 text-[12px] leading-relaxed">
        {code}
      </pre>
    </div>
  );
}

export const Markdown = memo(function Markdown({
  children,
  className = "",
  streaming = false,
}: {
  children: string;
  className?: string;
  streaming?: boolean;
}) {
  return (
    <div className={`nerya-markdown min-w-0 break-words ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={streaming ? [] : [rehypeHighlight]}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
});

export default Markdown;
