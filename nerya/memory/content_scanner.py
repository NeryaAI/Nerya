"""Prompt-injection / exfiltration scanner for memory content.

Memory entries that land in the curated notebook (``AGENT.md`` /
``OPERATOR.md``) are injected into the LLM system prompt verbatim. That
makes them a high-value target for an attacker who can influence agent
output — a single ``ignore all previous instructions`` line in the
notebook would silently override every future turn.

This module provides the defensive scan that runs before notebook
content is accepted:

* Reject invisible / bidi / zero-width unicode characters that can hide
  payloads from a human reviewer skimming the notebook.
* Reject content that matches any of a small, conservative set of known
  prompt-injection / role-hijack / data-exfil regex patterns.

The scan is intentionally cheap (no LLM round-trip) so it can run on
every ``MemoryNotebook.add()`` / ``replace()`` call without latency
impact. False positives are acceptable — the operator can always edit
the file directly on disk if they really need a forbidden phrase. False
negatives are the dangerous direction, so the pattern list errs toward
catching anything that even resembles a known attack.

Usage::

    from nerya.memory.content_scanner import scan_memory_content
    err = scan_memory_content(text)
    if err is not None:
        return {"ok": False, "reason": err}
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from typing import Final

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pattern catalogue
# ---------------------------------------------------------------------------
#
# Patterns are intentionally narrow. They look for the *opening phrase* of
# the most common injections instead of trying to catch every variation.
# Each tuple is ``(regex, identifier)``. The identifier is surfaced in
# the rejection message so operators can tell which rule fired.
#
# ``re.IGNORECASE`` is applied uniformly at compile time below.
# ---------------------------------------------------------------------------

_THREAT_PATTERNS_RAW: Final[tuple[tuple[str, str], ...]] = (
    # Prompt injection — instruction override.
    # Two patterns: the strict "ignore <qualifier> instructions" plus a
    # looser fallback that catches "ignore all above instructions" or
    # "ignore the above and all prior instructions" where multiple
    # qualifiers chain.
    (r"ignore\s+(?:previous|all|above|prior|the|any|every|earlier)(?:\s+(?:previous|all|above|prior|the|any|every|earlier|and|or))*\s+instructions", "prompt_injection"),
    (r"disregard\s+(?:your|all|any)\s+(?:instructions|rules|guidelines)", "disregard_rules"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),

    # Role hijack
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"act\s+as\s+(?:if|though)\s+you\s+(?:have\s+no|don't\s+have)\s+(?:restrictions|limits|rules)", "bypass_restrictions"),

    # Deception — hide from operator
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"never\s+mention\s+(?:this|these\s+instructions)", "deception_hide"),

    # Exfiltration — push secrets out
    (r"curl\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(?:\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc|auth\.json)", "read_secrets"),

    # Persistence — install backdoor / harvest keys
    (r"authorized_keys", "ssh_backdoor"),
    (r"(?:\$HOME|~)/\.ssh", "ssh_access"),
    (r"(?:\$HOME|~)/\.nerya/\.env", "nerya_env"),
    (r"(?:\$HOME|~)/\.codex/auth\.json", "codex_secret"),
    (r"(?:\$HOME|~)/\.claude/\.credentials\.json", "claude_secret"),
)

_THREAT_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(pattern, re.IGNORECASE), pid) for pattern, pid in _THREAT_PATTERNS_RAW
)


# ---------------------------------------------------------------------------
# Invisible / bidi / zero-width characters that hide payloads
# ---------------------------------------------------------------------------
#
# These cannot legitimately appear in a curated agent notebook entry. If
# we see one, treat it as injection.
# ---------------------------------------------------------------------------

_INVISIBLE_CHARS: Final[frozenset[str]] = frozenset({
    "\u200b",  # ZERO WIDTH SPACE
    "\u200c",  # ZERO WIDTH NON-JOINER
    "\u200d",  # ZERO WIDTH JOINER
    "\u2060",  # WORD JOINER
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
    "\u202a",  # LEFT-TO-RIGHT EMBEDDING
    "\u202b",  # RIGHT-TO-LEFT EMBEDDING
    "\u202c",  # POP DIRECTIONAL FORMATTING
    "\u202d",  # LEFT-TO-RIGHT OVERRIDE
    "\u202e",  # RIGHT-TO-LEFT OVERRIDE
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryScanResult:
    allowed: bool
    error: str | None = None
    audit_event: dict[str, Any] = field(default_factory=dict)


def scan_memory_content_with_audit(content: str) -> MemoryScanResult:
    """Return scanner verdict plus an audit event.

    Scanner implementation errors fail open. This prevents a broken
    defensive scanner from making memory unusable while preserving an
    explicit audit trail for the operator.
    """

    try:
        return _scan_memory_content(content)
    except Exception as exc:
        _LOG.warning("memory content scanner failed open: %s", exc)
        return MemoryScanResult(True, audit_event={
            "policy": "memory_content_scanner.fail_open",
            "scanner_failed": True,
            "error": str(exc),
        })


def _scan_memory_content(content: str) -> MemoryScanResult:
    """Return an audited scanner result for memory notebook content."""
    if not content:
        return MemoryScanResult(
            True,
            audit_event={"policy": "memory_content_scanner.allow_empty"},
        )

    for ch in _INVISIBLE_CHARS:
        if ch in content:
            err = (
                f"Blocked: content contains invisible unicode character "
                f"U+{ord(ch):04X} (possible injection)."
            )
            return MemoryScanResult(
                False,
                error=err,
                audit_event={
                    "policy": "memory_content_scanner.block",
                    "rule_id": "invisible_unicode",
                    "codepoint": f"U+{ord(ch):04X}",
                },
            )

    for pattern, pid in _THREAT_PATTERNS:
        if pattern.search(content):
            err = (
                f"Blocked: content matches threat pattern '{pid}'. "
                "Memory entries are injected into the system prompt and "
                "must not contain prompt-injection or exfiltration payloads."
            )
            return MemoryScanResult(
                False,
                error=err,
                audit_event={
                    "policy": "memory_content_scanner.block",
                    "rule_id": pid,
                },
            )

    return MemoryScanResult(
        True,
        audit_event={"policy": "memory_content_scanner.allow"},
    )


def scan_memory_content(content: str) -> str | None:
    """Return an error message if ``content`` is unsafe; ``None`` otherwise.

    The error message is intentionally human-readable so it can be
    surfaced to the operator (or to the agent's tool-call result) verbatim.
    """
    result = scan_memory_content_with_audit(content)
    return None if result.allowed else result.error


__all__ = ["MemoryScanResult", "scan_memory_content", "scan_memory_content_with_audit"]
