"use client";

import { useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import styles from "./WorkflowCodeEditor.module.css";

/** Native textarea editing with a non-interactive syntax layer. The original
 * string is the sole saved value; rendered markup is never converted to code. */
export function WorkflowCodeEditor({ value, onChange, readOnly, label }: {
  value: string; onChange: (value: string) => void; readOnly?: boolean; label: string;
}) {
  const text = useRef<HTMLTextAreaElement>(null);
  const syntax = useRef<HTMLDivElement>(null);
  const lines = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ line: 1, column: 1 });
  const highlighted = useMemo(() => {
    const longest = (value.match(/`+/g) || []).reduce((n, run) => Math.max(n, run.length), 2);
    const fence = "`".repeat(longest + 1);
    return `${fence}python\n${value}\n${fence}`;
  }, [value]);
  function sync() {
    const editor = text.current;
    if (!editor) return;
    if (syntax.current) syntax.current.style.transform = `translate(${-editor.scrollLeft}px, ${-editor.scrollTop}px)`;
    if (lines.current) lines.current.style.transform = `translateY(${-editor.scrollTop}px)`;
  }
  function cursor() {
    const before = value.slice(0, text.current?.selectionStart || 0).split("\n");
    setPosition({ line: before.length, column: before[before.length - 1].length + 1 });
  }
  return <div className={styles.editor} data-testid="workflow-code-editor">
    <div className={styles.body}>
      <div className={styles.gutter} aria-hidden="true"><div ref={lines}>{value.split("\n").map((_, i) => <div key={i} data-active={i + 1 === position.line}>{i + 1}</div>)}</div></div>
      <div className={styles.surface}>
        <div className={styles.mirror} aria-hidden="true"><div ref={syntax} className="nerya-code">{value.length > 200000 ? <pre><code>{value + "\n"}</code></pre> : <ReactMarkdown rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}>{highlighted}</ReactMarkdown>}</div></div>
        <textarea ref={text} aria-label={label} value={value} readOnly={readOnly} onChange={(event) => onChange(event.target.value)} onScroll={sync} onSelect={cursor} spellCheck={false} autoComplete="off" autoCapitalize="off" autoCorrect="off" wrap="off" />
      </div>
    </div>
    <footer><span>Python</span><span>Ln {position.line}, Col {position.column}</span><span>{readOnly ? "只读 / Read-only" : "⌘ / Ctrl S"}</span></footer>
  </div>;
}
