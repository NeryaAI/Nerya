"""Convert agent markdown prose into the small HTML subset Telegram supports.

The agent's ``send_message`` output is normal CommonMark (headings,
bullet lists, fenced code blocks, tables, links). Telegram's Bot API
only renders a strict HTML subset (``<b>``, ``<i>``, ``<s>``, ``<u>``,
``<code>``, ``<pre>``, ``<a>``, ``<blockquote>``) when ``parse_mode=HTML``
is set, and outright rejects markdown that contains unescaped special
characters when ``parse_mode=MarkdownV2``.

Without conversion, sending raw markdown source verbatim makes code
blocks and lists look broken in the Telegram client. Every Telegram
body now runs through this helper before posting:

  >>> render_markdown_for_telegram("**bold** and `code`\n- one\n- two")
  '<b>bold</b> and <code>code</code>\n• one\n• two'

The implementation is intentionally regex-based and dependency free —
it covers the markdown features the agent actually emits without
pulling a full markdown parser into the messaging layer. Anything we
can't safely render (e.g. tables, raw HTML) falls back to plain text so
the user still sees the content.
"""

from __future__ import annotations

import html
import re


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*[-_*]{3,}\s*$")


def render_markdown_for_telegram(text: str) -> str:
    """Render *text* as Telegram-compatible HTML.

    Returns a string safe to send with ``parse_mode=HTML``. Newlines
    are preserved (Telegram honours ``\\n`` inside HTML messages).
    """

    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_fence = False
    fence_lang = ""
    fence_buf: list[str] = []
    in_quote = False
    quote_buf: list[str] = []

    def _flush_fence() -> None:
        nonlocal in_fence, fence_lang, fence_buf
        if not fence_buf:
            in_fence = False
            fence_lang = ""
            return
        body = html.escape("\n".join(fence_buf))
        if fence_lang:
            out.append(
                f'<pre><code class="language-{html.escape(fence_lang)}">'
                f"{body}</code></pre>"
            )
        else:
            out.append(f"<pre>{body}</pre>")
        in_fence = False
        fence_lang = ""
        fence_buf = []

    def _flush_quote() -> None:
        nonlocal in_quote, quote_buf
        if not quote_buf:
            in_quote = False
            return
        body = "\n".join(quote_buf)
        out.append(f"<blockquote>{_render_inline(body)}</blockquote>")
        quote_buf = []
        in_quote = False

    for raw in lines:
        if in_fence:
            if raw.strip() == "```":
                _flush_fence()
            else:
                fence_buf.append(raw)
            continue

        m_fence = _FENCE_RE.match(raw)
        if m_fence:
            _flush_quote()
            in_fence = True
            fence_lang = m_fence.group(1) or ""
            continue

        m_quote = _BLOCKQUOTE_RE.match(raw)
        if m_quote:
            in_quote = True
            quote_buf.append(m_quote.group(1))
            continue
        if in_quote:
            _flush_quote()

        if _HR_RE.match(raw):
            out.append("──────────")
            continue

        m_h = _HEADING_RE.match(raw)
        if m_h:
            label = m_h.group(2).strip()
            out.append(f"<b>{_render_inline(label)}</b>")
            continue

        m_bullet = _BULLET_RE.match(raw)
        if m_bullet:
            indent = m_bullet.group(1) or ""
            content = m_bullet.group(2)
            depth = len(indent) // 2
            prefix = "  " * depth + "•"
            out.append(f"{prefix} {_render_inline(content)}")
            continue

        m_num = _NUMBERED_RE.match(raw)
        if m_num:
            indent = m_num.group(1) or ""
            content = m_num.group(2)
            depth = len(indent) // 2
            prefix = "  " * depth + "•"
            out.append(f"{prefix} {_render_inline(content)}")
            continue

        out.append(_render_inline(raw))

    if in_fence:
        _flush_fence()
    if in_quote:
        _flush_quote()

    return "\n".join(out).rstrip()


_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")
_ITALIC_RE = re.compile(
    r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<![\w_])_([^_\n]+)_(?![\w_])"
)
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^\s)]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\"'>])\bhttps?://[^\s<>\")]+", re.IGNORECASE)


def _render_inline(text: str) -> str:
    r"""Render the inline span of a markdown line as Telegram HTML.

    Order of operations:
      1. Pull out fenced inline code (``\`code\``) into placeholders so
         the bold/italic regexes can't dive into them.
      2. Escape the raw HTML.
      3. Apply bold/italic/strike + link conversions on the escaped
         shell.
      4. Re-emit code as ``<code>…</code>``.
    """

    code_slots: list[str] = []

    def _stash_code(m: re.Match[str]) -> str:
        idx = len(code_slots)
        code_slots.append(m.group(1))
        return f"\x00CODE{idx}\x00"

    masked = _INLINE_CODE_RE.sub(_stash_code, text)
    escaped = html.escape(masked, quote=False)

    escaped = _BOLD_RE.sub(
        lambda m: f"<b>{m.group(1) or m.group(2)}</b>", escaped,
    )
    escaped = _ITALIC_RE.sub(
        lambda m: f"<i>{m.group(1) or m.group(2)}</i>", escaped,
    )
    escaped = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", escaped)
    escaped = _LINK_RE.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">'
                  f"{m.group(1)}</a>",
        escaped,
    )

    def _restore_code(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        body = html.escape(code_slots[idx], quote=False)
        return f"<code>{body}</code>"

    escaped = re.sub(r"\x00CODE(\d+)\x00", _restore_code, escaped)
    return escaped


__all__ = ["render_markdown_for_telegram"]
