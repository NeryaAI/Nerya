"""Session-level context checkpointing for long chat threads.

The native loop already compacts a single turn's provider transcript.
This module handles the other half of the problem: when a user resumes a
long session, older user/assistant pairs should not silently disappear just
because we only replay the recent tail.

The design mirrors Codex-style compaction at the session boundary:

* compact older transcript rows into a structured checkpoint;
* persist that checkpoint in ``agent_sessions.meta_json``;
* replay the checkpoint as the first synthetic user message, followed by the
  recent uncompressed tail.

The summarizer is intentionally deterministic and extractive. Tests and
offline runtime paths must not depend on an LLM call just to keep context
bounded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


SESSION_COMPACTION_META_KEY = "context_compaction"
CHECKPOINT_VERSION = 1
CHECKPOINT_HEADER = "[context checkpoint]"


@dataclass(frozen=True)
class SessionCompactionPolicy:
    """Controls when and how prior chat history is compacted."""

    keep_recent_pairs: int = 12
    trigger_pairs: int = 12
    per_message_chars: int = 12_000
    max_bullets_per_section: int = 12
    max_render_chars: int = 18_000

    @property
    def keep_recent_messages(self) -> int:
        return max(2, int(self.keep_recent_pairs) * 2)

    @property
    def trigger_messages(self) -> int:
        return max(self.keep_recent_messages, int(self.trigger_pairs) * 2)


@dataclass
class SessionCompactionResult:
    """Result of compacting prior session history for prompt replay."""

    messages: list[dict[str, Any]]
    checkpoint: dict[str, Any] | None = None
    compacted: bool = False
    folded_messages: int = 0


@dataclass
class _Digest:
    session_intent: list[str] = field(default_factory=list)
    user_requests: list[str] = field(default_factory=list)
    assistant_results: list[str] = field(default_factory=list)
    files_and_artifacts: list[str] = field(default_factory=list)
    decisions_and_constraints: list[str] = field(default_factory=list)
    open_threads: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "_Digest":
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            session_intent=_clean_list(raw.get("session_intent")),
            user_requests=_clean_list(raw.get("user_requests")),
            assistant_results=_clean_list(raw.get("assistant_results")),
            files_and_artifacts=_clean_list(raw.get("files_and_artifacts")),
            decisions_and_constraints=_clean_list(raw.get("decisions_and_constraints")),
            open_threads=_clean_list(raw.get("open_threads")),
        )

    def asdict(self) -> dict[str, list[str]]:
        return {
            "session_intent": list(self.session_intent),
            "user_requests": list(self.user_requests),
            "assistant_results": list(self.assistant_results),
            "files_and_artifacts": list(self.files_and_artifacts),
            "decisions_and_constraints": list(self.decisions_and_constraints),
            "open_threads": list(self.open_threads),
        }


_FILE_REF_RE = re.compile(
    r"(?P<backtick>`[^`\r\n]{1,220}\.(?:py|ts|tsx|js|jsx|md|json|yml|yaml|toml|rs|go|sh|ps1|sql|html|css)`)"
    r"|(?P<path>(?:[A-Za-z]:\\[^\s`'\"<>]+|(?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9_+-]+))"
)
_DECISION_RE = re.compile(
    r"\b(decided|decision|constraint|must|should|verified|tested|rejected|keep|preserve)\b"
    r"|决定|约束|必须|不要|保留|验证|测试",
    re.IGNORECASE,
)
_OPEN_RE = re.compile(
    r"\b(next|todo|remaining|follow[- ]?up|blocked|open)\b|下一步|待办|还需要|剩余|阻塞",
    re.IGNORECASE,
)


def checkpoint_from_session_meta(meta: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a valid stored checkpoint from session meta, if present."""

    if not isinstance(meta, Mapping):
        return None
    raw = meta.get(SESSION_COMPACTION_META_KEY)
    if not isinstance(raw, Mapping):
        return None
    if int(raw.get("version") or 0) != CHECKPOINT_VERSION:
        return None
    rendered = raw.get("rendered")
    if not isinstance(rendered, str) or not rendered.strip():
        return None
    return dict(raw)


def _coerce_checkpoint(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Accept either session meta or a raw checkpoint dict."""

    if not isinstance(raw, Mapping):
        return None
    if int(raw.get("version") or 0) == CHECKPOINT_VERSION:
        rendered = raw.get("rendered")
        if isinstance(rendered, str) and rendered.strip():
            return dict(raw)
    return checkpoint_from_session_meta(raw)


def compact_session_history(
    rows: Sequence[Mapping[str, Any]],
    *,
    existing_checkpoint: Mapping[str, Any] | None = None,
    policy: SessionCompactionPolicy | None = None,
    exclude_turn_id: str | None = None,
) -> SessionCompactionResult:
    """Build prompt-ready prior messages with an anchored checkpoint.

    ``rows`` are chronological rows from ``AgentSessionRepository.transcript``.
    The returned ``messages`` are provider-shaped ``{role, content}`` entries
    that can be prepended to the next turn's transcript.
    """

    pol = policy or SessionCompactionPolicy()
    clean = _normalise_rows(rows, exclude_turn_id=exclude_turn_id, cap=pol.per_message_chars)
    if not clean:
        return SessionCompactionResult(messages=[])
    if len(clean) <= pol.trigger_messages:
        return SessionCompactionResult(messages=_strip_internal(clean))

    tail_count = min(len(clean), pol.keep_recent_messages)
    fold_candidates = clean[:-tail_count]
    tail = clean[-tail_count:]
    checkpoint = _coerce_checkpoint(existing_checkpoint)
    digest = _Digest.from_mapping((checkpoint or {}).get("digest"))

    new_span = fold_candidates
    last_id = str((checkpoint or {}).get("last_compacted_message_id") or "")
    if checkpoint and last_id:
        for idx, item in enumerate(fold_candidates):
            if str(item.get("message_id") or "") == last_id:
                new_span = fold_candidates[idx + 1:]
                break
        else:
            # The transcript changed underneath the stored checkpoint
            # (message delete/edit, import, or old metadata). Regenerate from
            # the current persisted transcript rather than layering stale facts.
            digest = _Digest()
            new_span = fold_candidates

    _fold_into_digest(digest, new_span, limit=pol.max_bullets_per_section)
    last_folded = fold_candidates[-1]
    rendered = _render_checkpoint(
        digest,
        compacted_count=len(fold_candidates),
        first_message_id=str(fold_candidates[0].get("message_id") or ""),
        last_message_id=str(last_folded.get("message_id") or ""),
        max_chars=pol.max_render_chars,
    )
    updated_checkpoint = {
        "version": CHECKPOINT_VERSION,
        "rendered": rendered,
        "digest": digest.asdict(),
        "compacted_message_count": len(fold_candidates),
        "first_compacted_message_id": str(fold_candidates[0].get("message_id") or ""),
        "last_compacted_message_id": str(last_folded.get("message_id") or ""),
        "last_compacted_turn_id": str(last_folded.get("turn_id") or ""),
        "last_compacted_ts": float(last_folded.get("ts") or 0.0),
    }
    messages = [{"role": "user", "content": rendered}] + _strip_internal(tail)
    return SessionCompactionResult(
        messages=messages,
        checkpoint=updated_checkpoint,
        compacted=True,
        folded_messages=len(fold_candidates),
    )


def _normalise_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    exclude_turn_id: str | None,
    cap: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        if exclude_turn_id and str(row.get("turn_id") or "") == str(exclude_turn_id):
            continue
        role = str(row.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        text = content[: max(1, int(cap))]
        out.append({
            "role": role,
            "content": text,
            "message_id": str(row.get("message_id") or f"row:{idx}"),
            "turn_id": str(row.get("turn_id") or ""),
            "ts": float(row.get("ts") or 0.0),
        })
    return out


def _strip_internal(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": str(item.get("role") or ""), "content": str(item.get("content") or "")}
        for item in items
        if str(item.get("role") or "") in ("user", "assistant")
        and str(item.get("content") or "").strip()
    ]


def _fold_into_digest(digest: _Digest, messages: Sequence[Mapping[str, Any]], *, limit: int) -> None:
    if not messages:
        _trim_digest(digest, limit=limit)
        return
    for item in messages:
        role = str(item.get("role") or "")
        text = str(item.get("content") or "")
        snippet = _first_signal_line(text)
        if not snippet:
            continue
        if role == "user":
            if not digest.session_intent:
                _append_unique(digest.session_intent, snippet, limit=limit)
            _append_unique(digest.user_requests, snippet, limit=limit)
        elif role == "assistant":
            _append_unique(digest.assistant_results, snippet, limit=limit)
        for ref in _extract_file_refs(text):
            _append_unique(digest.files_and_artifacts, ref, limit=limit)
        for line in _signal_lines(text, _DECISION_RE):
            _append_unique(digest.decisions_and_constraints, line, limit=limit)
        for line in _signal_lines(text, _OPEN_RE):
            _append_unique(digest.open_threads, line, limit=limit)
    _trim_digest(digest, limit=limit)


def _render_checkpoint(
    digest: _Digest,
    *,
    compacted_count: int,
    first_message_id: str,
    last_message_id: str,
    max_chars: int,
) -> str:
    sections = [
        (
            CHECKPOINT_HEADER,
            [
                "Earlier turns in this same Nerya session were compacted.",
                "Continue as if the compacted turns are still present; use the recent uncompressed tail below for exact wording.",
            ],
        ),
        ("Session Intent", digest.session_intent or ["(not enough signal captured)"]),
        ("Key User Requests", digest.user_requests),
        ("Assistant Results", digest.assistant_results),
        ("Files And Artifacts", digest.files_and_artifacts),
        ("Decisions And Constraints", digest.decisions_and_constraints),
        ("Open Threads", digest.open_threads),
        (
            "Checkpoint Stats",
            [
                f"compacted_messages={compacted_count}",
                f"range={first_message_id}..{last_message_id}",
            ],
        ),
    ]
    lines: list[str] = []
    for title, values in sections:
        lines.append(f"## {title}" if title != CHECKPOINT_HEADER else title)
        if not values:
            lines.append("- (none captured)")
        else:
            for value in values:
                lines.append(f"- {_clip(value, 360)}")
        lines.append("")
    rendered = "\n".join(lines).strip()
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max(512, max_chars - 96)].rstrip() + "\n\n[checkpoint truncated to fit prompt budget]"


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _append_unique(target: list[str], value: str, *, limit: int) -> None:
    text = _clip(value.strip(), 420)
    if not text:
        return
    if text in target:
        return
    target.append(text)
    if len(target) > limit:
        del target[0: len(target) - limit]


def _trim_digest(digest: _Digest, *, limit: int) -> None:
    for values in (
        digest.session_intent,
        digest.user_requests,
        digest.assistant_results,
        digest.files_and_artifacts,
        digest.decisions_and_constraints,
        digest.open_threads,
    ):
        if len(values) > limit:
            del values[0: len(values) - limit]


def _first_signal_line(text: str) -> str:
    for line in text.splitlines():
        clean = _clean_line(line)
        if clean:
            return clean
    return ""


def _signal_lines(text: str, pattern: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        clean = _clean_line(line)
        if clean and pattern.search(clean):
            out.append(clean)
        if len(out) >= 4:
            break
    return out


def _extract_file_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in _FILE_REF_RE.finditer(text):
        raw = match.group("backtick") or match.group("path") or ""
        raw = raw.strip("`.,;:)(")
        if raw and raw not in refs:
            refs.append(raw)
        if len(refs) >= 16:
            break
    return refs


def _clean_line(line: str) -> str:
    clean = re.sub(r"\s+", " ", str(line or "")).strip()
    clean = clean.strip("-*#> ")
    return _clip(clean, 420)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


__all__ = [
    "CHECKPOINT_HEADER",
    "SESSION_COMPACTION_META_KEY",
    "SessionCompactionPolicy",
    "SessionCompactionResult",
    "checkpoint_from_session_meta",
    "compact_session_history",
]
