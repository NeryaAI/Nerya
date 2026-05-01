"""Token-aware context compression + reference store.

Two public surfaces:

* :func:`estimate_tokens` — cheap heuristic (``~chars / 4``) that avoids a
  hard tiktoken dependency. For OpenAI/Anthropic models the real ratio is
  ~3.5-4.5 chars/token, so this is a usable upper bound.
* :func:`compress` — given an ordered list of ``(label, text)`` segments
  and a ``budget_tokens`` limit, drop or shrink segments until the total
  fits. Returns the surviving segments plus a ``dropped`` list.

The companion :class:`ReferenceStore` writes any dropped segment to
``workspace/context/references/<ref_id>.txt`` so the caller can put a short
``[ref: <id>]`` placeholder in the prompt and the operator / LLM can pull
the full text later via ``load_reference``.

Ported concept-for-concept from Hermes' ``context_compressor.py`` +
``context_references.py`` but kept dependency-free.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..core.atomic_write import atomic_write_text


# ---------------------------------------------------------------- tokens
def estimate_tokens(text: str) -> int:
    """Rough token count using a 4-chars-per-token heuristic."""
    if not text:
        return 0
    # A word-ish split gives a tighter lower bound; we max it with the
    # char-based estimate so very long single tokens still count correctly.
    chars = len(text)
    words = len(re.findall(r"\S+", text))
    return max(words, (chars + 3) // 4)


# ---------------------------------------------------------------- compress
@dataclass
class Segment:
    label: str
    text: str
    priority: int = 0
    droppable: bool = True

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class CompressionResult:
    kept: list[Segment] = field(default_factory=list)
    dropped: list[Segment] = field(default_factory=list)
    shrunk: list[Segment] = field(default_factory=list)
    total_tokens: int = 0
    budget_tokens: int = 0


def _truncate_by_tokens(text: str, max_tokens: int) -> str:
    """Approximate char-level truncate to keep roughly ``max_tokens`` tokens."""
    if max_tokens <= 0 or not text:
        return ""
    approx_chars = max(64, max_tokens * 4)
    if len(text) <= approx_chars:
        return text
    head = text[: approx_chars - 40]
    return head + "\n...[truncated]"


def compress(segments: Iterable[Segment], *, budget_tokens: int,
             keep_tail: int = 1) -> CompressionResult:
    """Drop / shrink ``segments`` until the total fits under ``budget_tokens``.

    Drop policy (in order):
    1. Drop lowest-priority, droppable segments first.
    2. If still over budget, shrink droppable segments starting from the
       middle (keep the last ``keep_tail`` and the first).
    3. Non-droppable segments are never dropped but may still be shrunk as a
       last resort.
    """
    seg_list = list(segments)
    result = CompressionResult(kept=list(seg_list), budget_tokens=budget_tokens)
    total = sum(s.tokens for s in seg_list)
    result.total_tokens = total
    if total <= budget_tokens:
        return result

    # step 1: drop lowest-priority droppable
    drop_candidates = sorted(
        [s for s in result.kept if s.droppable],
        key=lambda s: (s.priority, s.tokens),
    )
    for s in drop_candidates:
        if result.total_tokens <= budget_tokens:
            break
        result.kept.remove(s)
        result.dropped.append(s)
        result.total_tokens -= s.tokens

    # step 2: shrink droppable (keep tail)
    if result.total_tokens > budget_tokens:
        tail = result.kept[-keep_tail:] if keep_tail else []
        body = result.kept[:-keep_tail] if keep_tail else result.kept
        for idx, s in enumerate(body):
            if not s.droppable:
                continue
            if result.total_tokens <= budget_tokens:
                break
            overflow = result.total_tokens - budget_tokens
            target = max(32, s.tokens - overflow - 16)
            new_text = _truncate_by_tokens(s.text, target)
            if new_text == s.text:
                continue
            new_seg = Segment(label=s.label, text=new_text,
                              priority=s.priority, droppable=s.droppable)
            result.kept[result.kept.index(s)] = new_seg
            result.shrunk.append(new_seg)
            result.total_tokens = sum(x.tokens for x in result.kept)
        _ = tail  # tail is preserved by not iterating

    # step 3: shrink non-droppable if still over
    if result.total_tokens > budget_tokens:
        for s in list(result.kept):
            if result.total_tokens <= budget_tokens:
                break
            overflow = result.total_tokens - budget_tokens
            target = max(32, s.tokens - overflow - 16)
            new_text = _truncate_by_tokens(s.text, target)
            if new_text == s.text:
                continue
            new_seg = Segment(label=s.label, text=new_text,
                              priority=s.priority, droppable=s.droppable)
            result.kept[result.kept.index(s)] = new_seg
            result.shrunk.append(new_seg)
            result.total_tokens = sum(x.tokens for x in result.kept)

    return result


# ---------------------------------------------------------------- reference store
@dataclass
class ReferenceStore:
    """Content-addressed store for dropped / shrunk context segments.

    Writes each reference as ``<workspace>/context/references/<sha>.txt``
    and returns a short id the caller can embed in the prompt. Reads are
    idempotent and safe to hand out to operators.
    """

    root: Path

    @property
    def refs_dir(self) -> Path:
        return self.root / "context" / "references"

    def save(self, text: str, *, label: str = "") -> str:
        if not text:
            return ""
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        p = self.refs_dir / f"{sha}.txt"
        if not p.exists():
            header = f"# label: {label}\n" if label else ""
            atomic_write_text(p, header + text)
        return sha

    def load(self, ref_id: str) -> str | None:
        if not ref_id:
            return None
        p = self.refs_dir / f"{ref_id}.txt"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def list(self) -> list[str]:
        if not self.refs_dir.exists():
            return []
        return sorted(p.stem for p in self.refs_dir.iterdir()
                      if p.is_file() and p.suffix == ".txt")


def compress_with_refs(
    segments: Iterable[Segment],
    *,
    budget_tokens: int,
    store: ReferenceStore,
    keep_tail: int = 1,
) -> tuple[CompressionResult, dict[str, str]]:
    """Compress + write every dropped segment into the reference store.

    Returns (result, {label: ref_id}).
    """
    result = compress(segments, budget_tokens=budget_tokens, keep_tail=keep_tail)
    refs: dict[str, str] = {}
    for s in result.dropped:
        refs[s.label] = store.save(s.text, label=s.label)
    return result, refs


def budget_for_model(
    *,
    provider: str,
    model_id: str,
    reserve_output: int = 4_096,
    fallback_budget: int = 8_192,
    headroom_ratio: float = 0.85,
) -> int:
    """Plan 25 §5 — derive a compression budget from the model registry
    instead of the legacy hardcoded ``8_192`` fallback.

    The budget is::

        max(fallback_budget, int((context_window - reserve_output) * headroom_ratio))

    so an LLM with a 200k context window gets a much larger working
    surface than ``light-model``.  ``reserve_output`` is the minimum
    we keep free for the response, and ``headroom_ratio`` leaves a few
    % of the context as a safety margin against tokenizer drift.
    Returns ``fallback_budget`` whenever the model is unknown.
    """

    from .model_registry import lookup as _lookup

    meta = _lookup(provider, model_id)
    cw = meta.context_window or 0
    if cw <= 0:
        return max(fallback_budget, 0)
    usable = cw - max(reserve_output, 0)
    if usable <= 0:
        return max(fallback_budget, 0)
    headroom = max(0.0, min(headroom_ratio, 1.0))
    budget = int(usable * headroom)
    return max(budget, fallback_budget)
