"""Transcript-aware compaction with tool_use / tool_result invariants.

Nerya already had TTL-based markdown memory compaction. This module adds
transcript-aware compaction that respects tool-use/tool-result pairs.

Design goals (kept small and dependency-free on purpose):

- A transcript is a list of dict messages. Each message has at minimum
  ``role`` (``user|assistant|tool``) and one or more content entries.
- ``tool_use`` entries introduce a ``tool_use_id``; their paired
  ``tool_result`` entries reference the same id.
- When we compact, we must **never** drop a ``tool_use`` without also
  dropping its ``tool_result`` (and vice versa). Splitting a pair
  produces an internally invalid transcript that makes providers
  error out at the next turn.
- We always keep:
  * the **last `keep_tail_messages`** messages (recent context wins),
  * any message with a ``sticky: true`` / ``pinned: true`` flag
    (operator-pinned decisions, approvals, risk gates),
  * messages belonging to ``protected_turn_ids`` (explicit caller ask),
  * all messages containing invoked-skill summary envelopes
    (``kind == "skill_envelope"``), so skill state survives compaction
    even when free-form chatter is pruned.
- We always evict in pairs (tool_use + tool_result together).
- When we evict a chunk, we leave a single summary message in its
  place so downstream readers know something was compacted.

This module is pure-python and does not touch disk by itself; the
agent kernel / session store decides when to run it and where to
persist the result. That keeps the test surface tight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class CompactionReport:
    """Outcome of one compaction pass."""

    kept: int = 0
    dropped: int = 0
    pairs_dropped: int = 0
    skills_preserved: list[str] = field(default_factory=list)
    summary_inserted: bool = False

    def asdict(self) -> dict[str, Any]:
        return {
            "kept": self.kept,
            "dropped": self.dropped,
            "pairs_dropped": self.pairs_dropped,
            "skills_preserved": list(self.skills_preserved),
            "summary_inserted": self.summary_inserted,
        }


# ---------------------------------------------------------------------------
# transcript helpers
# ---------------------------------------------------------------------------


def _content_entries(msg: dict[str, Any]) -> list[dict[str, Any]]:
    content = msg.get("content")
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    if isinstance(content, dict):
        return [content]
    return []


def _tool_use_ids(msg: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for c in _content_entries(msg):
        if c.get("type") == "tool_use":
            tid = c.get("id") or c.get("tool_use_id")
            if tid:
                ids.add(str(tid))
    return ids


def _tool_result_ids(msg: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for c in _content_entries(msg):
        if c.get("type") == "tool_result":
            tid = c.get("tool_use_id") or c.get("id")
            if tid:
                ids.add(str(tid))
    return ids


def _invoked_skill_names(msg: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for c in _content_entries(msg):
        if c.get("type") == "skill_envelope":
            name = c.get("skill") or c.get("skill_id")
            if name:
                names.append(str(name))
    return names


def _is_pinned(msg: dict[str, Any]) -> bool:
    return bool(msg.get("sticky") or msg.get("pinned"))


def _is_system(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "system"


_FILE_REF_RE = re.compile(
    r"`(?P<bt>[^`\r\n]{1,180}\.(?:py|ts|tsx|js|jsx|md|json|yml|yaml|toml|rs|go|sh|ps1|sql|html|css))`"
    r"|(?P<path>(?:[A-Za-z]:\\[^\s`'\"<>]+|(?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9_+-]+))"
)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif item.get("type") == "tool_use":
                name = item.get("name") or "tool"
                parts.append(f"tool_use:{name}")
            elif item.get("type") == "tool_result":
                inner = item.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    for part in inner:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            parts.append(str(part["text"]))
        return "\n".join(parts)
    return ""


def _compact_line(text: str, *, limit: int = 220) -> str:
    for line in str(text or "").splitlines():
        clean = re.sub(r"\s+", " ", line).strip(" -*#>")
        if clean:
            return clean[: max(1, limit - 3)].rstrip() + "..." if len(clean) > limit else clean
    return ""


def _extract_file_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in _FILE_REF_RE.finditer(text):
        ref = (match.group("bt") or match.group("path") or "").strip("`.,;:)(")
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= 8:
            break
    return refs


def _summarize_evicted_run(
    messages: list[dict[str, Any]],
    indices: list[int],
    *,
    summary_prefix: str,
) -> str:
    selected = [messages[i] for i in indices if 0 <= i < len(messages)]
    roles: dict[str, int] = {}
    tools: list[str] = []
    user_lines: list[str] = []
    assistant_lines: list[str] = []
    file_refs: list[str] = []
    for msg in selected:
        role = str(msg.get("role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
        content = msg.get("content")
        text = _text_from_content(content)
        if role == "user":
            line = _compact_line(text)
            if line and line not in user_lines:
                user_lines.append(line)
        elif role == "assistant":
            line = _compact_line(text)
            if line and line not in assistant_lines:
                assistant_lines.append(line)
        for entry in _content_entries(msg):
            if entry.get("type") == "tool_use":
                name = str(entry.get("name") or "")
                if name and name not in tools:
                    tools.append(name)
        for ref in _extract_file_refs(text):
            if ref not in file_refs:
                file_refs.append(ref)

    lines = [
        f"{summary_prefix} dropped {len(indices)} message(s) between positions {indices[0]} and {indices[-1]}.",
        "Preserved summary of the dropped span:",
    ]
    if roles:
        lines.append("- roles: " + ", ".join(f"{k}={v}" for k, v in sorted(roles.items())))
    if user_lines:
        lines.append("- user signals: " + " | ".join(user_lines[:3]))
    if assistant_lines:
        lines.append("- assistant signals: " + " | ".join(assistant_lines[:3]))
    if tools:
        lines.append("- tools: " + ", ".join(tools[:8]))
    if file_refs:
        lines.append("- files/artifacts: " + ", ".join(file_refs[:8]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_transcript(messages: list[dict[str, Any]]) -> list[str]:
    """Return a list of invariant violations (empty = healthy).

    Currently enforced invariants:
    1. Every ``tool_use`` has a matching ``tool_result`` later in the
       transcript.
    2. Every ``tool_result`` has a prior ``tool_use``.
    3. Messages are well-formed (``role`` present).
    """
    violations: list[str] = []
    seen_uses: set[str] = set()
    seen_results: set[str] = set()
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or "role" not in m:
            violations.append(f"msg[{i}]: missing role")
            continue
        seen_uses.update(_tool_use_ids(m))
        for rid in _tool_result_ids(m):
            if rid not in seen_uses:
                violations.append(f"msg[{i}]: tool_result {rid!r} without prior tool_use")
            seen_results.add(rid)
    dangling = seen_uses - seen_results
    for did in sorted(dangling):
        violations.append(f"tool_use {did!r} never had a tool_result")
    return violations


# ---------------------------------------------------------------------------
# compaction
# ---------------------------------------------------------------------------


def _pair_groups(messages: list[dict[str, Any]]) -> dict[str, set[int]]:
    """Group message indices that belong to the same tool_use id pair.

    A single tool_use + tool_result pair can live across 2 (or more)
    messages; any eviction must take every index in the group, or
    keep every index in the group. We compute that closure once up
    front.
    """
    groups: dict[str, set[int]] = {}
    for i, m in enumerate(messages):
        for tid in _tool_use_ids(m) | _tool_result_ids(m):
            groups.setdefault(tid, set()).add(i)
    return groups


def compact_transcript(
    messages: list[dict[str, Any]],
    *,
    keep_tail_messages: int = 20,
    max_messages: int | None = None,
    protected_turn_ids: Iterable[str] = (),
    keep_system: bool = True,
    summary_prefix: str = "[compacted]",
) -> tuple[list[dict[str, Any]], CompactionReport]:
    """Compact a transcript while preserving invariants.

    Parameters
    ----------
    messages:
        The current transcript (list of dict messages).
    keep_tail_messages:
        Always keep at least this many of the most recent messages.
    max_messages:
        If the transcript is already at or below this count, return
        it unchanged. Defaults to ``keep_tail_messages * 2``.
    protected_turn_ids:
        Messages carrying ``turn_id`` in this set are always kept.
    keep_system:
        If True, system messages are always preserved.
    summary_prefix:
        Prefix used on the synthetic breadcrumb message inserted
        when anything was evicted.
    """
    report = CompactionReport()
    if not messages:
        return messages, report
    max_messages = max_messages if max_messages is not None else keep_tail_messages * 2
    if len(messages) <= max_messages:
        report.kept = len(messages)
        return list(messages), report

    protected = set(str(t) for t in (protected_turn_ids or ()))
    n = len(messages)
    tail_start = max(0, n - keep_tail_messages)
    pair_groups = _pair_groups(messages)

    keep_idx: set[int] = set(range(tail_start, n))
    preserved_skills: list[str] = []

    for i, m in enumerate(messages):
        if i in keep_idx:
            skills = _invoked_skill_names(m)
            if skills:
                preserved_skills.extend(skills)
            continue
        if keep_system and _is_system(m):
            keep_idx.add(i)
            continue
        if _is_pinned(m):
            keep_idx.add(i)
            continue
        if str(m.get("turn_id", "")) in protected:
            keep_idx.add(i)
            continue
        skills = _invoked_skill_names(m)
        if skills:
            keep_idx.add(i)
            preserved_skills.extend(skills)

    # Pair-closure: if we keep one index in a pair group, keep them all.
    # If we drop one, drop them all.
    for group in pair_groups.values():
        if group & keep_idx:
            keep_idx |= group
        # groups fully outside keep_idx stay dropped as a unit

    dropped_idx = [i for i in range(n) if i not in keep_idx]
    pairs_dropped = 0
    for group in pair_groups.values():
        if group.isdisjoint(keep_idx):
            pairs_dropped += 1

    out: list[dict[str, Any]] = []
    evicted_run: list[int] = []

    def flush_run() -> None:
        nonlocal evicted_run
        if not evicted_run:
            return
        # produce one breadcrumb for this contiguous eviction run
        breadcrumb: dict[str, Any] = {
            "role": "system",
            "kind": "transcript.compact.breadcrumb",
            "content": _summarize_evicted_run(
                messages,
                evicted_run,
                summary_prefix=summary_prefix,
            ),
            "meta": {"dropped_indices": list(evicted_run)},
        }
        out.append(breadcrumb)
        report.summary_inserted = True
        evicted_run = []

    for i, m in enumerate(messages):
        if i in keep_idx:
            flush_run()
            out.append(m)
        else:
            evicted_run.append(i)
    flush_run()

    report.kept = sum(1 for m in out if m.get("kind") != "transcript.compact.breadcrumb")
    report.dropped = len(dropped_idx)
    report.pairs_dropped = pairs_dropped
    # de-dup while preserving order
    seen: set[str] = set()
    for name in preserved_skills:
        if name not in seen:
            seen.add(name)
            report.skills_preserved.append(name)

    # Post-condition: compaction result must validate.
    assert not validate_transcript(out), \
        f"compaction produced invalid transcript: {validate_transcript(out)}"
    return out, report
