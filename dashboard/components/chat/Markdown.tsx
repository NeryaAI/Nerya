"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { Components } from "react-markdown";

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
      href={href}
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
    <th className="px-2 py-1 text-left border-b border-ink-700/60 font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="px-2 py-1 border-b border-ink-700/40 text-ink-100">
      {children}
    </td>
  ),
  code(props) {
    const { className, children, ...rest } = props as {
      className?: string;
      children?: React.ReactNode;
      inline?: boolean;
    };
    const inline = (props as { inline?: boolean }).inline;
    if (inline) {
      return (
        <code className="px-1 py-0.5 rounded bg-ink-800/70 text-brand-200 font-mono text-[12px]">
          {children}
        </code>
      );
    }
    return (
      <code className={`${className ?? ""} font-mono text-[12px]`} {...rest}>
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="bg-ink-900/70 border border-ink-700/60 rounded-md p-3 overflow-x-auto my-2 text-[12px] leading-relaxed">
      {children}
    </pre>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-ink-50">{children}</strong>
  ),
  em: ({ children }) => <em className="italic text-ink-100">{children}</em>,
};

export function Markdown({
  children,
  className = "",
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={`nerya-markdown ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

export default Markdown;
