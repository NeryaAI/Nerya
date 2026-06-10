"""Artifact index.

At the end of every coding turn the runtime emits an autonomous summary
listing the files that were modified, the commands that were run, the
errors encountered, and any unverified risks (for example, "I changed
``config.yml`` but never re-ran the test that depends on it"). This
summary is independent of whatever the model puts in its final text —
operators rely on it for spot checks.

We compute the same digest from the per-turn :class:`BlockEnvelope`
list. The kernel attaches the result to ``AgentTurnResult`` and
journals it under ``agent.turn.summary`` so dashboards / CI gates
can pull it without re-parsing the transcript.

Categories tracked:

* **Created files** — ``write_file`` calls where the path didn't
  previously exist (we don't have an authoritative pre-state, so
  this falls back to "any ``write_file`` call that succeeded").
* **Modified files** — ``edit_file`` / ``write_file`` calls.
* **Read files** — distinct ``read_file`` paths (so the operator
  can confirm "yes, the agent grounded its edits in the right
  context").
* **Commands** — ``run_shell`` calls and their exit code.
* **Tests** — commands whose pattern matches a test runner (handled
  by :mod:`nerya.agent.verifier`'s catalogue). We re-derive locally
  to keep the modules independent.
* **Errors** — every ``tool_result`` that came back ``is_error=True``,
  with the kind, the tool, and the (truncated) message.
* **Unverified risks** — heuristics that pair edits with their
  expected verification:

  - file edited but never re-read → "stale anchor risk"
  - file edited but no test invoked that touches its directory →
    "unverified change"
  - destructive shell command (``rm``, ``DELETE``, ``DROP``) without
    a follow-up read → "destructive without verification"

The output is intentionally a flat dict so it serialises cleanly
into the journal and the API ``AgentTurnResult.artifact_index``
field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable


_TEST_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bpytest\b",
        r"\bpython\s+-m\s+(?:unittest|pytest)\b",
        r"\bgo\s+test\b",
        r"\bcargo\s+test\b",
        r"\bnpm\s+(?:run\s+)?test\b",
        r"\byarn\s+(?:run\s+)?test\b",
        r"\bpnpm\s+(?:run\s+)?test\b",
        r"\bmake\s+(?:test|check|verify)\b",
        r"\bjest\b",
        r"\bvitest\b",
    )
)

_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+-rf?\b",
        r"\bdrop\s+(?:table|database|schema)\b",
        r"\bdelete\s+from\b",
        r"\btruncate\b",
        r"\bdd\s+if=",
        r"\bmkfs\b",
        r"\bsudo\s+rm\b",
        r"\bgit\s+(?:reset|clean)\s+-",
    )
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ArtifactIndex:
    """Auto-collected per-turn summary."""

    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    tests_run: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    recovered_errors: list[dict[str, Any]] = field(default_factory=list)
    unverified_risks: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "created": list(self.created),
            "modified": list(self.modified),
            "read": list(self.read),
            "commands": list(self.commands),
            "tests_run": list(self.tests_run),
            "errors": list(self.errors),
            "recovered_errors": list(self.recovered_errors),
            "unverified_risks": list(self.unverified_risks),
            "counters": dict(self.counters),
        }

    def render_markdown(self) -> str:
        """Render a compact markdown summary for the dashboard / CLI."""

        lines: list[str] = ["## Turn artifact index"]
        if self.created or self.modified:
            lines.append("")
            lines.append("**Files touched**")
            for p in self.created:
                lines.append(f"- created: `{p}`")
            for p in self.modified:
                if p in self.created:
                    continue
                lines.append(f"- modified: `{p}`")
        if self.read:
            lines.append("")
            lines.append(f"**Files read**: {len(self.read)} distinct path(s)")
        if self.commands:
            lines.append("")
            lines.append("**Commands**")
            for c in self.commands:
                exit_code = c.get("exit_code")
                cmd = c.get("command") or ""
                cmd_short = cmd if len(cmd) <= 80 else cmd[:80] + "…"
                lines.append(f"- `[exit={exit_code}]` `{cmd_short}`")
        if self.errors:
            lines.append("")
            lines.append("**Errors**")
            for e in self.errors:
                lines.append(
                    f"- {e.get('tool')}: {e.get('kind')} — "
                    f"{e.get('message') or ''}"
                )
        if self.recovered_errors:
            lines.append("")
            lines.append("**Recovered errors**")
            for e in self.recovered_errors:
                lines.append(
                    f"- {e.get('tool')}: {e.get('kind')} — "
                    f"{e.get('message') or ''}"
                )
        if self.unverified_risks:
            lines.append("")
            lines.append("**Unverified risks**")
            for r in self.unverified_risks:
                lines.append(f"- {r.get('kind')}: {r.get('detail') or ''}")
        if not lines[1:]:  # only header so far
            lines.append("(no file-system changes, no commands, no errors)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _normalize_path(p: str) -> str:
    """Canonicalise so de-dup works across windows / posix mixed inputs."""

    if not p:
        return ""
    try:
        return str(PurePosixPath(p.replace("\\", "/")))
    except Exception:
        return p


def _is_test_command(command: str) -> bool:
    return any(p.search(command or "") for p in _TEST_COMMAND_PATTERNS)


def _is_destructive_command(command: str) -> bool:
    return any(p.search(command or "") for p in _DESTRUCTIVE_PATTERNS)


def _is_tool_redirect_result(result: dict[str, Any]) -> bool:
    recovery = result.get("recovery")
    if not isinstance(recovery, dict):
        return False
    return str(recovery.get("reason") or "").strip().lower() == "tool_redirect"


def _recovery_key(action: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return a stable target key for retry recovery accounting."""

    if action in {"run_shell", "script_run"}:
        return None
    for key in (
        "task_id",
        "id",
        "name",
        "strategy_id",
        "proposal_id",
        "account_id",
        "provider",
        "venue",
        "market",
        "symbol",
        "path",
        "url",
        "target",
    ):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return (action, f"{key}:{text}")
    return None


def _tool_error_entry(action: str, result: dict[str, Any]) -> dict[str, Any]:
    err_dict = result.get("error") or {}
    return {
        "tool": action,
        "kind": err_dict.get("kind") if isinstance(err_dict, dict) else None,
        "message": (err_dict.get("message") if isinstance(err_dict, dict) else None)
        or str(result.get("error") or "")[:200],
    }


def build_artifact_index(blocks: Iterable[dict[str, Any]]) -> ArtifactIndex:
    """Walk a turn's blocks and emit an :class:`ArtifactIndex`.

    ``blocks`` is the same shape :class:`AgentTurnResult.blocks` carries
    (each entry is the dict form of a :class:`BlockEnvelope`).
    """

    index = ArtifactIndex()
    edited_paths: list[str] = []
    edited_set: set[str] = set()
    read_paths: list[str] = []
    read_set: set[str] = set()
    created_set: set[str] = set()
    destructive_commands_without_followup: list[dict[str, Any]] = []

    # ---- pass 1: collect raw events -----------------------------------
    events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for env in blocks or ():
        block = env.get("block") if isinstance(env, dict) else None
        block = block if isinstance(block, dict) else env
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind not in {"tool_use", "tool_result"}:
            continue
        events.append((kind, block, env if isinstance(env, dict) else {}))

    # tool_use → its tool_use_id → matching tool_result (same envelope
    # ordering as the orchestrator surfaces them).
    #
    # The harness emits ``call_id`` on both the ``tool_use`` block (the
    # provider id of the call) and the matching ``tool_result`` block.
    # ``block_id`` is per-block and uses different prefixes for use vs
    # result, so we cannot pair on it. We fall back to the older
    # ``id`` / ``tool_use_id`` names so legacy callers keep working.
    tool_uses: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    for kind, block, _env in events:
        cid = str(
            block.get("call_id")
            or block.get("tool_use_id")
            or block.get("id")
            or ""
        )
        if not cid:
            continue
        if kind == "tool_use":
            tool_uses[cid] = block
        else:
            tool_results[cid] = block

    attempts: list[tuple[str, dict[str, Any], dict[str, Any], bool, tuple[str, str] | None]] = []
    successful_positions: dict[tuple[str, str], list[int]] = {}
    for bid, use in tool_uses.items():
        action = str(use.get("action") or "")
        payload = use.get("payload") or {}
        payload = payload if isinstance(payload, dict) else {}
        result = tool_results.get(bid) or {}
        ok = bool(result.get("ok", True))
        key = _recovery_key(action, payload)
        pos = len(attempts)
        attempts.append((action, payload, result, ok, key))
        if ok and key:
            successful_positions.setdefault(key, []).append(pos)

    # ---- pass 2: classify ---------------------------------------------
    for pos, (action, payload, result, ok, key) in enumerate(attempts):

        if action == "read_file":
            p = _normalize_path(str(payload.get("path") or ""))
            if p and p not in read_set:
                read_set.add(p)
                read_paths.append(p)
        elif action in {"edit_file", "write_file"}:
            p = _normalize_path(str(payload.get("path") or ""))
            if not p or not ok:
                continue
            if p not in edited_set:
                edited_set.add(p)
                edited_paths.append(p)
            if action == "write_file":
                # Heuristic: if the same path was *not* read prior to
                # the write within this turn, treat it as freshly
                # created (model can't have inspected pre-existing
                # content). Otherwise we conservatively call it
                # modified.
                if p not in read_set:
                    created_set.add(p)
        elif action == "run_shell":
            command = str(payload.get("command") or "")
            if not _is_tool_redirect_result(result):
                shell_data = result.get("output") or {}
                exit_code = shell_data.get("exit_code") if isinstance(shell_data, dict) else None
                entry = {
                    "command": command,
                    "exit_code": exit_code,
                    "ok": ok,
                }
                index.commands.append(entry)
                if _is_test_command(command):
                    index.tests_run.append(entry)
                if _is_destructive_command(command):
                    destructive_commands_without_followup.append(entry)
        elif action in {"script_run"}:
            entry = {
                "script": payload.get("name"),
                "ok": ok,
            }
            index.commands.append(entry)
        # error tracking
        if not ok and not _is_tool_redirect_result(result):
            entry = _tool_error_entry(action, result)
            recovered = key is not None and any(
                success_pos > pos for success_pos in successful_positions.get(key, [])
            )
            if recovered:
                index.recovered_errors.append(entry)
            else:
                index.errors.append(entry)

    index.created = sorted(created_set)
    index.modified = [p for p in edited_paths if p not in created_set]
    index.read = read_paths

    # ---- pass 3: derive risks -----------------------------------------
    test_count = len(index.tests_run)

    for p in edited_paths:
        post_edit_read = False
        # We re-walk events to check for read AFTER the edit.
        seen_edit = False
        for kind, block, _env in events:
            if kind != "tool_use":
                continue
            action = str(block.get("action") or "")
            payload = block.get("payload") or {}
            path = _normalize_path(str(payload.get("path") or ""))
            if not seen_edit:
                if action in {"edit_file", "write_file"} and path == p:
                    seen_edit = True
                continue
            if action == "read_file" and path == p:
                post_edit_read = True
                break
        if not post_edit_read and test_count == 0:
            index.unverified_risks.append(
                {
                    "kind": "edit_without_verification",
                    "detail": (
                        f"{p}: edited but never re-read and no test "
                        f"runner invoked this turn"
                    ),
                }
            )
        elif not post_edit_read:
            index.unverified_risks.append(
                {
                    "kind": "edit_without_reread",
                    "detail": (
                        f"{p}: edited but never re-read; verify the "
                        f"final on-disk bytes match intent"
                    ),
                }
            )

    for cmd in destructive_commands_without_followup:
        if cmd.get("ok") is False:
            continue
        index.unverified_risks.append(
            {
                "kind": "destructive_without_verification",
                "detail": (
                    f"command `{(cmd.get('command') or '')[:80]}` ran "
                    f"to completion; nothing in this turn checked the "
                    f"resulting state"
                ),
            }
        )

    # ---- counters ------------------------------------------------------
    index.counters = {
        "created": len(index.created),
        "modified": len(index.modified),
        "read": len(index.read),
        "commands": len(index.commands),
        "tests_run": len(index.tests_run),
        "errors": len(index.errors),
        "recovered_errors": len(index.recovered_errors),
        "unverified_risks": len(index.unverified_risks),
    }
    return index


def summarize_batch(
    *,
    results: Iterable[Any],
) -> dict[str, Any]:
    """Compact per-batch summary used for streaming events / dashboard. calls out a "tool batch summary": the dashboard wants a
    one-line status label for each batch the orchestrator just
    finished (e.g. ``"3 reads, 1 edit (1 retry)"``) without having to
    walk the transcript for it. The kernel publishes this on
    ``StreamingEventBus`` as ``tool.batch.summary`` after each
    ``run_batch`` call.

    ``results`` is the :class:`BatchResult.results` list (a sequence
    of :class:`~nerya.tools.types.ToolResult`). Returns a dict with
    counts + a one-line label.
    """

    items = list(results)
    counts = {
        "total": len(items),
        "ok": 0,
        "errors": 0,
        "by_tool": {},  # type: ignore[var-annotated]
    }
    by_tool: dict[str, dict[str, int]] = counts["by_tool"]  # type: ignore[assignment]
    for r in items:
        ok = not bool(getattr(r, "is_error", False))
        if ok:
            counts["ok"] += 1
        else:
            counts["errors"] += 1
        name = str(getattr(r, "name", "")) or "?"
        bucket = by_tool.setdefault(name, {"ok": 0, "errors": 0})
        if ok:
            bucket["ok"] += 1
        else:
            bucket["errors"] += 1

    parts: list[str] = []
    for name, b in sorted(by_tool.items()):
        if not b["errors"]:
            parts.append(f"{b['ok']}× {name}")
        elif not b["ok"]:
            parts.append(f"{b['errors']}× {name} (err)")
        else:
            parts.append(f"{b['ok']}× {name} (+{b['errors']} err)")
    label = ", ".join(parts) if parts else "(no tools)"
    return {
        "label": label,
        "total": counts["total"],
        "ok": counts["ok"],
        "errors": counts["errors"],
        "by_tool": by_tool,
    }


def render_final_report(index: ArtifactIndex) -> dict[str, Any]:
    """Render an :class:`ArtifactIndex` into the *final report* shape.

    the agent's final answer should be informed by the
    artifact index, not by what the model thinks it did. Dashboards /
    final-message renderers read this dict to surface a structured
    summary block under the natural-language final text. We keep the
    flat dict so it serialises into JSON envelopes without further
    massaging.
    """

    return {
        "headline": _final_report_headline(index),
        "files": {
            "created": list(index.created),
            "modified": list(index.modified),
            "read_count": len(index.read),
        },
        "commands": [
            {
                "command": (c.get("command") or "")[:160],
                "exit_code": c.get("exit_code"),
                "ok": c.get("ok", True),
            }
            for c in index.commands
        ],
        "tests_run": list(index.tests_run),
        "errors": [
            {
                "tool": e.get("tool"),
                "kind": e.get("kind"),
                "message": (e.get("message") or "")[:200],
            }
            for e in index.errors
        ],
        "recovered_errors": [
            {
                "tool": e.get("tool"),
                "kind": e.get("kind"),
                "message": (e.get("message") or "")[:200],
            }
            for e in index.recovered_errors
        ],
        "unverified_risks": list(index.unverified_risks),
        "counters": dict(index.counters),
    }


def _final_report_headline(index: ArtifactIndex) -> str:
    """One-line headline for the final report block."""

    bits: list[str] = []
    if index.created:
        bits.append(f"created {len(index.created)}")
    if index.modified:
        bits.append(f"modified {len(index.modified)}")
    if index.commands:
        bits.append(f"{len(index.commands)} command(s)")
    if index.errors:
        bits.append(f"{len(index.errors)} error(s)")
    if index.unverified_risks:
        bits.append(f"{len(index.unverified_risks)} unverified")
    if not bits:
        return "no file changes, no commands, no errors"
    return ", ".join(bits)


__all__ = [
    "ArtifactIndex",
    "build_artifact_index",
    "render_final_report",
    "summarize_batch",
]
